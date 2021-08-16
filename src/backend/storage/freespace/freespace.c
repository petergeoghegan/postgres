/*-------------------------------------------------------------------------
 *
 * freespace.c
 *	  POSTGRES free space map for managing free space in heap relations
 *
 *
 * Portions Copyright (c) 1996-2021, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * IDENTIFICATION
 *	  src/backend/storage/freespace/freespace.c
 *
 *
 * NOTES:
 *
 *	Free Space Map keeps track of free space lists.  See README for more
 *	information.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "access/heapam.h"
#include "access/hio.h"
#include "catalog/catalog.h"
#include "miscadmin.h"
#include "storage/freespace.h"
#include "storage/ipc.h"
#include "storage/lmgr.h"
#include "utils/relfilenodemap.h"

#ifdef USE_ASSERT_CHECKING
/* BEGIN temp debug stuff */
#define DEBUGLOG
#define DEBUGLEVEL1 DEBUG1
#define DEBUGLEVEL2 DEBUG1
/* END temp debug stuff */
#endif

/*
 * TODO: Make number of free lists configurable (storage param?)
 */
#define FSM_MAX_BLOCKS_PER_FREELIST		1
#define FSM_MAX_FREELISTS_PER_RELATION	1
#define FSM_NOBULK_REL_BLOCKS			1

static int	fsm_max_nrelations = 10000; /* max # relations to track */


/*
 * Global shared state
 */
typedef struct FSMSharedState
{
	int64		foo;

} FSMSharedState;

/* Links to shared memory state */
static FSMSharedState *fsm_shared_state = NULL;
static HTAB *fsm_hash = NULL;

/*
 * Individual free list -- each relation holds one or more of these in shared
 * memory
 */
typedef struct FSMFreeList
{
	int16		nextblockoff;	/* Offset to next consumable block */
	int16		nblocksalloc;	/* Space in shared mem */
	int			ownerpid;
	FullTransactionId ownerxid;

	/* Stats and other info for instrumentation */
	int64		nblocksconsumed;	/* Number of satisified block requests */
	int64		nrefreshes;		/* # rel extension ops */
	FullTransactionId leaderxid;	/* Just for instrumentation */

	/* Consumable blocks follow (interpreted using nextblockoff) */
	BlockNumber blocks[FSM_MAX_BLOCKS_PER_FREELIST];
} FSMFreeList;

typedef struct FSMRelationHashKey
{
	RelFileNode relfilenode;	/* Hashable relfilenode */
} FSMRelationHashKey;

typedef struct FSMRelation
{
	FSMRelationHashKey key;		/* hash key of entry - MUST BE FIRST */

	BlockNumber relnblocks;		/* For RelationGetNumberOfBlocks() */

	/* Stats and other info for instrumentation */
	int64		relnumextensionops;
	FullTransactionId rellastleaderxid;
	int64		dbgreltotal;	/* Just complain once */

	/* Consumable free lists follow */
	int			nfreelists;
	FSMFreeList freelists[FSM_MAX_FREELISTS_PER_RELATION];
} FSMRelation;

static FSMRelation *FSMGetRelation(Relation rel, bool reset);
static BlockNumber FSMRelationReset(Relation rel, BlockNumber nblocks);


/******** Public API ********/

/*
 * GetPageWithFreeSpace - try to find a page in the given relation with
 *		at least the specified amount of free space.
 *
 * If successful, return the block number; if not, return InvalidBlockNumber.
 *
 * The caller must be prepared for the possibility that the returned page
 * will turn out to have too little space available by the time the caller
 * gets a lock on it.  In that case, the caller should report the actual
 * amount of free space available on that page and then try again (see
 * RecordAndGetPageWithFreeSpace).  If InvalidBlockNumber is returned,
 * extend the relation.
 *
 * FIXME: We ignore spaceNeeded param -- we look for whole pages.
 */
BlockNumber
GetPageWithFreeSpace(Relation rel, Size spaceNeeded, BulkInsertState bistate)
{
	FullTransactionId XactTopFullTransactionId;
	FSMRelation *relfreelists;
	uint32		targetlist;
	int			bestlist = -1;
	FullTransactionId oldestownerxid = InvalidFullTransactionId;

	XactTopFullTransactionId = GetTopFullTransactionId();

	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);

	/* Find FSM entry for relation */
	relfreelists = FSMGetRelation(rel, false);

	if (!relfreelists)
	{
		/* FIXME: Currently index AMs and stuff go through here */
		LWLockRelease(FSMListLock);
		return InvalidBlockNumber;
	}

	/*
	 * Determine free list that this backend has affinity for currently, using
	 * relcache information
	 */
	targetlist = RelationGetSmgr(rel)->smgr_targlist;
	if (targetlist == PG_UINT32_MAX)
	{
		/* Not set up yet -- use default/initial criteria */
		targetlist = MyProcPid % relfreelists->nfreelists;
		RelationGetSmgr(rel)->smgr_targlist = targetlist;
	}

	/*
	 * Scan free lists for this relation.  We don't jump straight to our own
	 * because we might not be able to use it -- we look for a second-best
	 * option in passing.  Backend's targetlist can change when we fail to get
	 * a usable block from existing free list tied that was previous tied to
	 * the backend.
	 */
retry:
	for (int i = 0; i < relfreelists->nfreelists; i++)
	{
		FSMFreeList *list = relfreelists->freelists + i;
		int			nusable_blocks = list->nblocksalloc - list->nextblockoff;

		Assert(nusable_blocks >= 0);
		Assert(nusable_blocks <= FSM_MAX_BLOCKS_PER_FREELIST);

		if (i != targetlist)
		{
			if (nusable_blocks > 0 &&
				FullTransactionIdFollows(XactTopFullTransactionId, list->ownerxid) &&
				(!FullTransactionIdIsValid(oldestownerxid) ||
				 FullTransactionIdFollows(oldestownerxid, list->ownerxid)))
			{

				oldestownerxid = list->ownerxid;
				bestlist = i;
			}
		}
		else if (nusable_blocks > 0)
		{
			/*
			 * Success!
			 *
			 * Found our list, which is usable by backend -- it is good for at
			 * least one block, and likely many more
			 */
			BlockNumber newblock = list->blocks[list->nextblockoff++];

			list->nblocksconsumed++;
			list->ownerpid = MyProcPid;
			list->ownerxid = XactTopFullTransactionId;

			LWLockRelease(FSMListLock);

			return newblock;
		}
	}

	/*
	 * We failed to get a new block for caller.  If we noticed a next-best
	 * option during scan, then go with that by retrying scan.
	 *
	 * XXX: This whole approach seems pretty circuitous.
	 */
	if (bestlist >= 0)
	{
		targetlist = bestlist;
		RelationGetSmgr(rel)->smgr_targlist = targetlist;
		goto retry;
	}

	LWLockRelease(FSMListLock);

	return InvalidBlockNumber;
}

/*
 * Returns buffer for leader backend
 */
Buffer
FreeSpaceMapAddExtraBlocks(Relation rel, Size spaceNeeded,
						   BulkInsertState bistate)
{
	FullTransactionId XactTopFullTransactionId = GetTopFullTransactionId();
	int			blocksPerFreelist;
	int			newnfreelists;
	FSMRelation *relfreelists;
	bool		leaderbufferfound = false;
	BlockNumber newleaderblock = InvalidBlockNumber;
	Buffer		buffer;
	Page		page;

	/* find freelists for rel */
	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);
	relfreelists = FSMGetRelation(rel, false);

	if (relfreelists->relnblocks < FSM_NOBULK_REL_BLOCKS &&
		relfreelists->nfreelists <= 1)
	{
		newnfreelists = 1;
		blocksPerFreelist = Max(1, relfreelists->relnblocks * 2);
		blocksPerFreelist = Min(blocksPerFreelist, 16);

		if (relfreelists->relnblocks > 0 && relfreelists->relnblocks < 10000)
		{
			BlockNumber lastblock = relfreelists->relnblocks - 1;
			Size		pageFreeSpace;

			buffer = ReadBufferBI(rel, lastblock, RBM_NORMAL, bistate);
			page = BufferGetPage(buffer);
			LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);
			pageFreeSpace = PageGetHeapFreeSpace(page);
			if (spaceNeeded <= pageFreeSpace)
			{
				LWLockRelease(FSMListLock);
				RelationSetTargetBlock(rel, lastblock);
				return buffer;
			}
		}
	}
	else
	{
		/* Disable lockwaiters thing for now -- just hard code */
#if 0
		int			lockWaiters;

		/* Use the length of the lock wait queue to judge how much to extend. */
		lockWaiters = RelationExtensionLockWaiterCount(rel);
		if (lockWaiters <= 0)
			return;
#endif
		newnfreelists = FSM_MAX_FREELISTS_PER_RELATION;
		blocksPerFreelist = FSM_MAX_BLOCKS_PER_FREELIST;
	}

	relfreelists->relnumextensionops++;
	relfreelists->rellastleaderxid = XactTopFullTransactionId;

#ifdef DEBUGLOG
	elog(DEBUGLEVEL1, "FreeSpaceMapAddExtraBlocks: rel %s newnfreelists %d blocksPerFreelist %d",
		 RelationGetRelationName(rel), newnfreelists, blocksPerFreelist);
#endif

	for (int i = 0; i < newnfreelists; i++)
	{
		FSMFreeList *flist = relfreelists->freelists + i;

		/*
		 * Skip over any of the rel's free lists that are not already
		 * exhausted, except when we're initializing it for the first time.
		 *
		 * The authoritative nfreelists from shared memory (which we test
		 * here) will be updated below.
		 */
		if (i >= relfreelists->nfreelists ||
			flist->nextblockoff == flist->nblocksalloc)
		{
			/* Reset fields for this free list */
			flist->nextblockoff = 0;
			flist->nblocksalloc = 0;
			flist->ownerpid = 0;
			flist->ownerxid = FirstNormalFullTransactionId;

			/*
			 * FIXME What about case where leader produces single list with
			 * single block, which leader itself consumes immediately?
			 */
			flist->nrefreshes++;
			flist->leaderxid = XactTopFullTransactionId;
			for (int j = 0; j < blocksPerFreelist; j++)
			{
				BlockNumber blockNum;

				/*
				 * Extend this free list by a single page.
				 *
				 * Maintain per-list count of pages allocated.  Also maintain
				 * size of relation in shared memory for
				 * RelationGetNumberOfBlocks().
				 *
				 * FIXME: That's actually a lie, at least right now.  Need to
				 * actually hook into smgr.c guts so that we maintain the
				 * authoritative information used by
				 * RelationGetNumberOfBlocks() within shared memory.  Exact
				 * boundaries unclear at this time.
				 */
				relfreelists->relnblocks++;
				flist->nblocksalloc++;

				/*
				 * This should generally match the main-line extension code in
				 * RelationGetBufferForTuple, except that we hold the relation
				 * extension lock throughout, and we don't immediately
				 * initialize the page (see below).
				 */
				buffer = ReadBufferBI(rel, P_NEW, RBM_ZERO_AND_LOCK, bistate);
				page = BufferGetPage(buffer);

				if (!PageIsNew(page))
					elog(ERROR, "page %u of relation \"%s\" should be empty but is not",
						 BufferGetBlockNumber(buffer),
						 RelationGetRelationName(rel));

				/*
				 * Add the page to the FSM without initializing. If we were to
				 * initialize here, the page would potentially get flushed out
				 * to disk before we add any useful content. There's no
				 * guarantee that that'd happen before a potential crash, so
				 * we need to deal with uninitialized pages anyway, thus avoid
				 * the potential for unnecessary writes.
				 */
				blockNum = BufferGetBlockNumber(buffer);
				UnlockReleaseBuffer(buffer);
				flist->blocks[j] = blockNum;

#ifdef DEBUGLOG
				elog(DEBUGLEVEL1, "FreeSpaceMapAddExtraBlocks loop: rel %s allocating block %u in freelist # %d relfreelists %d",
					 RelationGetRelationName(rel), blockNum, i, j);
#endif

				Assert(flist->nblocksalloc < PG_INT16_MAX);
			}
		}
		if (!leaderbufferfound)
		{
			Assert(flist->nextblockoff < flist->nblocksalloc);

			/* Leader must return a block for itself */
			RelationGetSmgr(rel)->smgr_targlist = i;
			newleaderblock = flist->blocks[flist->nextblockoff];
			if (relfreelists->relnblocks < 10000)
			{
				flist->ownerpid = 0;
				flist->ownerxid = FirstNormalFullTransactionId;
			}
			else
			{
				flist->nextblockoff++;
				flist->nblocksconsumed++;
				flist->ownerpid = MyProcPid;
				flist->ownerxid = XactTopFullTransactionId;
			}
			leaderbufferfound = true;
		}
	}

	/*
	 * Update the number of freelists, which may have gone up compared to the
	 * last call here
	 */
	relfreelists->nfreelists = newnfreelists;

	/*
	 * Return exclusively-locked buffer to leader backend -- this needs to
	 * happen before we release FSMListLock, so that forward progress is
	 * guaranteed under contention.
	 *
	 * XXX: Really?
	 */
	Assert(leaderbufferfound);
	buffer = ReadBufferBI(rel, newleaderblock, RBM_NORMAL, bistate);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);

	LWLockRelease(FSMListLock);

	RelationSetTargetBlock(rel, newleaderblock);

	return buffer;
}

/*
 * RecordAndGetPageWithFreeSpace - update info about a page and try again.
 *
 * We provide this combo form to save some locking overhead, compared to
 * separate RecordPageWithFreeSpace + GetPageWithFreeSpace calls. There's
 * also some effort to return a page close to the old page; if there's a
 * page with enough free space on the same FSM page where the old one page
 * is located, it is preferred.
 */
BlockNumber
RecordAndGetPageWithFreeSpace(Relation rel, BlockNumber oldPage,
							  Size oldSpaceAvail, Size spaceNeeded)
{
	return GetPageWithFreeSpace(rel, spaceNeeded, NULL);
}

/*
 * RecordPageWithFreeSpace - update info about a page.
 *
 * Note that if the new spaceAvail value is higher than the old value stored
 * in the FSM, the space might not become visible to searchers until the next
 * FreeSpaceMapVacuum call, which updates the upper level pages.
 */
void
RecordPageWithFreeSpace(Relation rel, BlockNumber heapBlk, Size spaceAvail)
{
}

/*
 * XLogRecordPageWithFreeSpace - like RecordPageWithFreeSpace, for use in
 *		WAL replay
 */
void
XLogRecordPageWithFreeSpace(RelFileNode rnode, BlockNumber heapBlk,
							Size spaceAvail)
{

}

/*
 * GetRecordedFreeSpace - return the amount of free space on a particular page,
 *		according to the FSM.
 *
 * XXX: Only core consumer is VACUUM
 */
Size
GetRecordedFreeSpace(Relation rel, BlockNumber heapBlk)
{
	return 0;
}

/*
 * FreeSpaceMapPrepareTruncateRel - prepare for truncation of a relation.
 *
 * nblocks is the new size of the heap.
 *
 * Return the number of blocks of new FSM.
 */
BlockNumber
FreeSpaceMapPrepareTruncateRel(Relation rel, BlockNumber nblocks)
{
	/*
	 * Note to self: Commit 917dc7d239 fixed a bug in VM truncation.  The
	 * considerations for a crash-safe FSM might be similar.
	 */
	/* FSMRelationReset(rel, nblocks); */
	return InvalidBlockNumber;
}

/*
 * FreeSpaceMapVacuum - update upper-level pages in the rel's FSM
 *
 * We assume that the bottom-level pages have already been updated with
 * new free-space information.
 */
void
FreeSpaceMapVacuum(Relation rel)
{
	/* FIXME */
}

/*
 * FreeSpaceMapVacuumRange - update upper-level pages in the rel's FSM
 *
 * As above, but assume that only heap pages between start and end-1 inclusive
 * have new free-space information, so update only the upper-level slots
 * covering that block range.  end == InvalidBlockNumber is equivalent to
 * "all the rest of the relation".
 */
void
FreeSpaceMapVacuumRange(Relation rel, BlockNumber start, BlockNumber end)
{
	if (end == InvalidBlockNumber)
		FSMRelationReset(rel, start);
}

Size
FreeSpaceMapShmemSize(void)
{
	Size		size;

	size = MAXALIGN(sizeof(FSMSharedState));
	size = add_size(size, hash_estimate_size(fsm_max_nrelations,
											 sizeof(FSMRelation)));

	return size;
}

void
FreeSpaceMapShmemInit(void)
{
	bool		found;
	HASHCTL		info;

	/* reset in case this is a restart within the postmaster */
	fsm_shared_state = NULL;
	fsm_hash = NULL;

	fsm_shared_state = ShmemInitStruct("fsm shared state",
									   sizeof(FSMSharedState), &found);

	if (!found)
	{
		/* First time through ... */
		fsm_shared_state->foo = 0;
	}

	/*
	 * Allocate hash table for relfilenodes.  This stores relation
	 * information.
	 */
	info.keysize = sizeof(FSMRelationHashKey);
	info.entrysize = sizeof(FSMRelation);
	info.num_partitions = NUM_FSMLOCK_PARTITIONS;
	fsm_hash = ShmemInitHash("fsm freelists hash",
							 fsm_max_nrelations, fsm_max_nrelations, &info,
							 HASH_ELEM | HASH_BLOBS | HASH_PARTITION |
							 HASH_FIXED_SIZE);
}

int64
FreeSpaceMapRelationGetNumberOfBlocks(RelFileNode rfn)
{

	FSMRelation frelfreelists;
	FSMRelation *relfreelists;
	int64		totalnblocksconsumed = 0;

	memset(&frelfreelists.key, 0, sizeof(FSMRelationHashKey));
	frelfreelists.key.relfilenode = rfn;

	LWLockAcquire(FSMListLock, LW_SHARED);

	/* Find or create an entry with desired hash code */
	relfreelists = (FSMRelation *) hash_search(fsm_hash, &frelfreelists.key,
											   HASH_FIND, NULL);

	if (relfreelists)
	{
		for (int i = 0; i < relfreelists->nfreelists; i++)
		{
			FSMFreeList *list = relfreelists->freelists + i;

			totalnblocksconsumed += list->nblocksconsumed;
		}
	}

	LWLockRelease(FSMListLock);

	return totalnblocksconsumed;
}

static FSMRelation *
FSMGetRelation(Relation rel, bool reset)
{
	FSMRelation key;
	FSMRelation *relfreelists;
	bool		found;

	/*
	 * No memset() needed -- we rely on the assumption that relfilenode has no
	 * padding bytes.  This is per RelFileNode struct's contract.
	 */
	key.key.relfilenode = rel->rd_node;

#if 0
	/* Make space if needed */
	while (hash_get_num_entries(fsm_hash) >= fsm_max_nrelations)
		relation_dealloc();
#endif

	/* Find or create an entry with desired hash code */
	relfreelists = (FSMRelation *) hash_search(fsm_hash, &key.key, HASH_ENTER,
											   &found);

	if (!found || reset)
	{
		/* New entry, initialize it */
		relfreelists->nfreelists = 1;
		relfreelists->relnumextensionops = 0;
		relfreelists->rellastleaderxid = InvalidFullTransactionId;
		relfreelists->dbgreltotal = -1;

		/*
		 * We track total relation size in shared memory
		 *
		 * FIXME: relnblocks field doesn't yet reliably agree with
		 * RelationGetNumberOfBlocks().
		 */
		relfreelists->relnblocks = RelationGetNumberOfBlocks(rel);
	}
#ifdef USE_ASSERT_CHECKING
	else
	{
		int64		totalnblocksconsumed = 0;
		int64		totalnblocksavailnow = 0;
		int64		nblocks = RelationGetNumberOfBlocks(rel);
		int64		reltotal;

		/* Assert(relfreelists->nfreelists >= 1); */
		Assert(relfreelists->nfreelists <= FSM_MAX_FREELISTS_PER_RELATION);

		for (int i = 0; i < relfreelists->nfreelists; i++)
		{
			FSMFreeList *list = relfreelists->freelists + i;

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksconsumed += list->nblocksconsumed;
			totalnblocksavailnow += list->nblocksalloc - list->nextblockoff;
		}

		reltotal = totalnblocksconsumed + totalnblocksavailnow;

		/*
		 * Pageinspect tests don't look at output, but try to catch egregious
		 * regressions here:
		 */
		if (nblocks != reltotal && reltotal != relfreelists->dbgreltotal)
		{
			if (!IsCatalogRelation(rel))
				elog(DEBUGLEVEL1, "%s leaked %lu (authoritative nblocks: %lu, reltotal: %lu)",
					 RelationGetRelationName(rel), nblocks - reltotal,
					 nblocks, reltotal);

			relfreelists->dbgreltotal = reltotal;	/* Just complain once */
		}
	}
#endif

	return relfreelists;
}

static BlockNumber
FSMRelationReset(Relation rel, BlockNumber nblocks)
{
	FSMRelation *relfreelists;

	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);
	relfreelists = FSMGetRelation(rel, true);
	if (relfreelists)
	{
		relfreelists->relnblocks = nblocks;
		relfreelists->nfreelists = 1;
		relfreelists->freelists[0].nextblockoff = relfreelists->freelists[0].nblocksalloc;
		relfreelists->freelists[0].nblocksconsumed = nblocks;
	}
	LWLockRelease(FSMListLock);
	return InvalidBlockNumber;
}

/*
 * Used by contrib/pageinspect to debug
 */
void
DebugFreeSpaceMapDump(Relation rel, StringInfo sinfo)
{
	FSMRelation frelfreelists;
	FSMRelation *relfreelists;
	int64		totalnblocksconsumed = 0;
	int64		totalnblocksavailnow = 0;
	int64		nblocks = RelationGetNumberOfBlocks(rel);

	memset(&frelfreelists.key, 0, sizeof(FSMRelationHashKey));
	frelfreelists.key.relfilenode = rel->rd_node;

	initStringInfo(sinfo);

	LWLockAcquire(FSMListLock, LW_SHARED);

	/* Find or create an entry with desired hash code */
	relfreelists = (FSMRelation *) hash_search(fsm_hash, &frelfreelists.key,
											   HASH_FIND, NULL);

	if (relfreelists)
	{
		RelFileNode relfilenode = relfreelists->key.relfilenode;
		int64		reltotal;

		appendStringInfo(sinfo, "%u/%u/%u:\n\n",
						 relfilenode.spcNode, relfilenode.dbNode,
						 relfilenode.relNode);

		for (int i = 0; i < relfreelists->nfreelists; i++)
		{
			FSMFreeList *list = relfreelists->freelists + i;

			appendStringInfo(sinfo, "    %d - nextblockoff: %d[blk %d]/%d, ownerxid: %u:%u, nblocksconsumed: %ld, nrefreshes: %ld, lastLeader: %u:%u\n",
							 i, list->nextblockoff, (list->nextblockoff == list->nblocksalloc ? -1 : list->blocks[list->nextblockoff]), list->nblocksalloc,
							 EpochFromFullTransactionId(list->ownerxid),
							 XidFromFullTransactionId(list->ownerxid),
							 list->nblocksconsumed, list->nrefreshes,
							 EpochFromFullTransactionId(list->leaderxid),
							 XidFromFullTransactionId(list->leaderxid));

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksconsumed += list->nblocksconsumed;
			totalnblocksavailnow += list->nblocksalloc - list->nextblockoff;
		}

		reltotal = totalnblocksconsumed + totalnblocksavailnow;
		appendStringInfo(sinfo, "  totalnblocksconsumed: %lu, totalnblocksavailnow: %lu (reltotal: %lu, RelationGetNumberOfBlocks()-wise leaked: %lu)\n",
						 totalnblocksconsumed, totalnblocksavailnow,
						 reltotal, nblocks - reltotal);
		appendStringInfo(sinfo, "rellastleaderxid: %u:%u, relnumextensionops: %lu\n",
						 EpochFromFullTransactionId(relfreelists->rellastleaderxid),
						 XidFromFullTransactionId(relfreelists->rellastleaderxid),
						 relfreelists->relnumextensionops);
		appendStringInfo(sinfo, "RelationGetNumberOfBlocks(): %lu, relfreelists->relnblocks: %u (these should match)\n",
						 nblocks, relfreelists->relnblocks);

		/*
		 * Pageinspect tests don't look at output, but try to catch egregious
		 * regressions here:
		 */
		if (nblocks != reltotal)
			elog(WARNING, "leaked %lu", nblocks - reltotal);
	}

	LWLockRelease(FSMListLock);
}

void
DebugFreeSpaceMapDumpAllRels(StringInfo sinfo)
{
	HASH_SEQ_STATUS hash_seq;
	FSMRelation *relfreelists = NULL;
	int			n = 0;

	initStringInfo(sinfo);

	LWLockAcquire(FSMListLock, LW_SHARED);

	Assert(fsm_hash != NULL);

	hash_seq_init(&hash_seq, fsm_hash);
	while ((relfreelists = hash_seq_search(&hash_seq)) != NULL)
	{
		Relation rel = NULL;
		RelFileNode relfilenode = relfreelists->key.relfilenode;
		Oid			reloid;
		int64		reltotal;
		int64		totalnblocksconsumed = 0;
		int64		totalnblocksavailnow = 0;

		reloid = RelidByRelfilenode(relfilenode.spcNode, relfilenode.relNode);
		if (OidIsValid(reloid))
			rel = try_relation_open(reloid, AccessShareLock);

		if (!rel)
		{
			appendStringInfo(sinfo, "rel # %d (unknown) %u/%u/%u has %d freelists:\n",
							 n++, relfilenode.spcNode, relfilenode.dbNode,
							 relfilenode.relNode,
							 relfreelists->nfreelists);
		}
		else
		{
			appendStringInfo(sinfo, "rel # %d (\"%s\"): %u/%u/%u has %d freelists:\n",
							 n++, RelationGetRelationName(rel),
							 relfilenode.spcNode, relfilenode.dbNode,
							 relfilenode.relNode,
							 relfreelists->nfreelists);
		}

		for (int i = 0; i < relfreelists->nfreelists; i++)
		{
			FSMFreeList *list = relfreelists->freelists + i;

			appendStringInfo(sinfo, "    %d - nextblockoff: %d[blk %d]/%d, ownerxid: %u:%u, nblocksconsumed: %ld, nrefreshes: %ld, lastLeader: %u:%u\n",
							 i, list->nextblockoff, (list->nextblockoff == list->nblocksalloc ? -1 : list->blocks[list->nextblockoff]), list->nblocksalloc,
							 EpochFromFullTransactionId(list->ownerxid),
							 XidFromFullTransactionId(list->ownerxid),
							 list->nblocksconsumed, list->nrefreshes,
							 EpochFromFullTransactionId(list->leaderxid),
							 XidFromFullTransactionId(list->leaderxid));

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksconsumed += list->nblocksconsumed;
			totalnblocksavailnow += list->nblocksalloc - list->nextblockoff;
		}

		reltotal = totalnblocksconsumed + totalnblocksavailnow;
		if (!rel)
			appendStringInfo(sinfo, "  totalnblocksconsumed: %lu, totalnblocksavailnow: %lu (reltotal: %lu)\n",
							 totalnblocksconsumed, totalnblocksavailnow,
							 reltotal);
		else
		{
			int64		nblocks = RelationGetNumberOfBlocks(rel);

			appendStringInfo(sinfo, "  totalnblocksconsumed: %lu, totalnblocksavailnow: %lu (reltotal: %lu, relationgetnumberofblocks()-wise leaked: %lu)\n",
							 totalnblocksconsumed, totalnblocksavailnow,
							 reltotal, nblocks - reltotal);
			if (nblocks != reltotal)
				elog(DEBUG1, "leaked %lu from rel \"%s\"", nblocks - reltotal,RelationGetRelationName(rel));
		}
		appendStringInfo(sinfo, "  rellastleaderxid: %u:%u, relnumextensionops: %lu\n\n",
						 EpochFromFullTransactionId(relfreelists->rellastleaderxid),
						 XidFromFullTransactionId(relfreelists->rellastleaderxid),
						 relfreelists->relnumextensionops);

		if (rel)
			relation_close(rel, AccessShareLock);
	}

	LWLockRelease(FSMListLock);
}
