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
#define FSM_MAX_SPACE_FOR_BLOCKS_PER_FREELIST		2048
#define FSM_MAX_BLOCKS_PER_FREELIST					128
#define FSM_MAX_FREELISTS_PER_RELATION				16
#define FSM_NOBULK_REL_BLOCKS						1024

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

typedef struct FSMFreeBlock
{
	BlockNumber		blk;
	bool			deleted;

} FSMFreeBlock;

/*
 * Individual free list -- each relation holds one or more of these in shared
 * memory
 */
typedef struct FSMFreeList
{
	int			nextblockoff;	/* Offset to next consumable block */
	int			nblocks_at_last_alloc;	/* Space in shared mem */
	int			ownerpid;
	FullTransactionId ownerxid;

	int64		nblocksalloced;		/* Number of blocks actually allocated on disk */
	int64		ndelblocks;			/* Number of deleted blocks received by this list */
	int64		nconsumedblocks;	/* Number of satisfied block requests */

	int64		nrefreshes;		/* # rel extension ops _or_ VACUUM ops */
	FullTransactionId leaderxid;	/* Just for instrumentation */

	/* Consumable blocks follow (interpreted using nextblockoff) */
	FSMFreeBlock blocks[FSM_MAX_SPACE_FOR_BLOCKS_PER_FREELIST];
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
	int64		relnumvacuumops;
	FullTransactionId rellastleaderxid;
	int64		dbgtotalnblocksalloced;	/* Just complain once */

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
	FSMRelation *fsmrel;
	uint32		targetlist;
	int			bestlist = -1;
	FullTransactionId oldestownerxid = InvalidFullTransactionId;

	XactTopFullTransactionId = GetTopFullTransactionId();

	/* TODO: Handle temp tables sensibly */
	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);

	/* Find FSM entry for relation */
	fsmrel = FSMGetRelation(rel, false);

	if (!fsmrel)
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
		targetlist = MyProcPid % fsmrel->nfreelists;
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
	for (int i = 0; i < fsmrel->nfreelists; i++)
	{
		FSMFreeList *flist = fsmrel->freelists + i;
		int			nusable_blocks = flist->nblocks_at_last_alloc - flist->nextblockoff;

		Assert(nusable_blocks >= 0);
		Assert(nusable_blocks <= FSM_MAX_SPACE_FOR_BLOCKS_PER_FREELIST);

		if (i != targetlist)
		{
			if (nusable_blocks > 0 &&
				FullTransactionIdFollows(XactTopFullTransactionId, flist->ownerxid) &&
				(!FullTransactionIdIsValid(oldestownerxid) ||
				 FullTransactionIdFollows(oldestownerxid, flist->ownerxid)))
			{

				oldestownerxid = flist->ownerxid;
				bestlist = i;
			}
		}
		else if (nusable_blocks > 0)
		{
			/*
			 * Success!
			 *
			 * Found our flist, which is usable by backend -- it is good for
			 * at least one block, and likely many more
			 */
			FSMFreeBlock newblock = flist->blocks[flist->nextblockoff++];

			flist->ownerpid = MyProcPid;
			flist->ownerxid = XactTopFullTransactionId;

			/* Do accounting */
			flist->nconsumedblocks++;
			if (newblock.deleted)
				flist->ndelblocks--;
			Assert(flist->ndelblocks >= 0);
			Assert(flist->nconsumedblocks >= 0);

			LWLockRelease(FSMListLock);

			return newblock.blk;
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

BlockNumber
BTreeGetIndexPageWithFreeSpace(Relation rel)
{
	return GetPageWithFreeSpace(rel, BLCKSZ, NULL);
}

/*
 * Returns buffer for leader backend
 */
Buffer
FreeSpaceMapAddExtraBlocks(Relation rel, Size spaceNeeded,
						   BulkInsertState bistate)
{
	FullTransactionId XactTopFullTransactionId = GetTopFullTransactionId();
	int			nblocks_per_freelist_this_alloc;
	int			newnfreelists;
	FSMRelation *fsmrel;
	FSMFreeBlock newleaderblock = {InvalidBlockNumber, false};
	Buffer		buffer;
	Page		page;

	/* find freelists for rel */
	/* TODO: Handle temp tables sensibly */
	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);
	fsmrel = FSMGetRelation(rel, false);

	if (fsmrel->relnblocks < FSM_NOBULK_REL_BLOCKS &&
		fsmrel->nfreelists <= 1 &&
		fsmrel->freelists[0].nblocks_at_last_alloc <= FSM_MAX_BLOCKS_PER_FREELIST)
	{
		newnfreelists = 1;
		nblocks_per_freelist_this_alloc =
			Max(1, fsmrel->relnblocks * 2);
		nblocks_per_freelist_this_alloc =
				Min(nblocks_per_freelist_this_alloc, 16);
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
		nblocks_per_freelist_this_alloc = FSM_MAX_BLOCKS_PER_FREELIST;
	}

	fsmrel->relnumextensionops++;
	fsmrel->rellastleaderxid = XactTopFullTransactionId;

#ifdef DEBUGLOG
	elog(DEBUGLEVEL1, "FreeSpaceMapAddExtraBlocks: rel %s newnfreelists %d nblocks_per_freelist_this_alloc %d",
		 RelationGetRelationName(rel), newnfreelists,
		 nblocks_per_freelist_this_alloc);
#endif

	for (int i = 0; i < newnfreelists; i++)
	{
		FSMFreeList *flist = fsmrel->freelists + i;

		/*
		 * Skip over any of the rel's free lists that are not already
		 * exhausted, except when we're initializing it for the first time.
		 *
		 * The authoritative nfreelists from shared memory (which we test
		 * here) will be updated below.
		 */
		if (i >= fsmrel->nfreelists ||
			flist->nextblockoff == flist->nblocks_at_last_alloc)
		{
			/* Reset fields for this free list */
			flist->nextblockoff = 0;
			/*  nblocks_at_last_alloc might be going up or down here: */
			flist->nblocks_at_last_alloc = nblocks_per_freelist_this_alloc;
			flist->ownerpid = 0;
			flist->ownerxid = FirstNormalFullTransactionId;

			/*
			 * FIXME What about case where leader produces single list with
			 * single block, which leader itself consumes immediately?
			 */
			flist->nrefreshes++;
			flist->leaderxid = XactTopFullTransactionId;
			for (int j = 0; j < nblocks_per_freelist_this_alloc; j++)
			{
				FSMFreeBlock newblock;

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
				fsmrel->relnblocks++;
				flist->nblocksalloced++;

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
				newblock.blk = BufferGetBlockNumber(buffer);
				newblock.deleted = false;
				UnlockReleaseBuffer(buffer);
				flist->blocks[j] = newblock;

#ifdef DEBUGLOG
				elog(DEBUGLEVEL1, "FreeSpaceMapAddExtraBlocks loop: rel %s allocating block %u in freelist # %d of fsmrel # %d",
					 RelationGetRelationName(rel), newblock.blk, i, j);
#endif
			}
		}
		if (!BlockNumberIsValid(newleaderblock.blk))
		{
			Assert(flist->nextblockoff < flist->nblocks_at_last_alloc);

			/* Leader must return a block for itself */
			RelationGetSmgr(rel)->smgr_targlist = i;
			newleaderblock = flist->blocks[flist->nextblockoff++];
			flist->nconsumedblocks++;
			flist->ownerpid = MyProcPid;
			flist->ownerxid = XactTopFullTransactionId;
		}
	}

	/*
	 * Update the number of freelists, which may have gone up compared to the
	 * last call here
	 */
	fsmrel->nfreelists = newnfreelists;

	/*
	 * Return exclusively-locked buffer to leader backend -- this needs to
	 * happen before we release FSMListLock, so that forward progress is
	 * guaranteed under contention.
	 *
	 * XXX: Really?
	 */
	buffer = ReadBufferBI(rel, newleaderblock.blk, RBM_NORMAL, bistate);
	LockBuffer(buffer, BUFFER_LOCK_EXCLUSIVE);

	LWLockRelease(FSMListLock);

	return buffer;
}

void
BTreeIndexFreeSpaceMapVacuum(Relation rel, BTVacState *vstate)
{
	FSMRelation *fsmrel;
	int			freeblockn = 0;

	/* find freelists for rel */
	/* TODO: Handle temp tables sensibly */
	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);
	fsmrel = FSMGetRelation(rel, false);

	fsmrel->relnumvacuumops++;

	for (int i = 0; i < fsmrel->nfreelists; i++)
	{
		FSMFreeList *flist = fsmrel->freelists + i;

		/*
		 * Skip over any of the rel's free lists that are not already
		 * exhausted, except when we're initializing it for the first time.
		 *
		 * The authoritative nfreelists from shared memory (which we test
		 * here) will be updated below.
		 */
		if (flist->nextblockoff == flist->nblocks_at_last_alloc)
		{
			/*
			 * Don't expand number of blocks per free list (stick with
			 * preexisting sizing of freelists).  Fill this empty list to that
			 * capacity (with deleted pages) once more.
			 */
			for (int j = 0; j < flist->nblocks_at_last_alloc; j++)
			{
				FSMFreeBlock newblock;

				if (vstate->npendingpages == freeblockn)
					break;

				/*
				 * Don't increment nblocksalloced here -- this is an existing
				 * block.  Just increment nblocksdeleted blocks received from
				 * VACUUM like this.
				 */
				if (j == 0)
				{
					flist->nextblockoff = 0;
					flist->ownerpid = 0;
					flist->nrefreshes++;
					flist->leaderxid = InvalidFullTransactionId;
					flist->ownerxid = FirstNormalFullTransactionId;
				}
				newblock.blk = vstate->pendingpages[freeblockn++].target;
				newblock.deleted = true;
				flist->blocks[j] = newblock;

				/* Do accounting */
				flist->ndelblocks++;
				flist->nconsumedblocks--;
				Assert(flist->ndelblocks >= 0);
				Assert(flist->nconsumedblocks >= 0);
			}
		}
	}

	if (vstate->npendingpages > freeblockn)
	{
		/*
		 * We have more deleted pages than we can use to backfill the empty
		 * lists.  So we're going to backfill non-empty lists now.
		 *
		 * We're even going to grow the lists considerably in the process.
		 */
		for (int i = 0; i < fsmrel->nfreelists; i++)
		{
			FSMFreeList *flist = fsmrel->freelists + i;

			if (flist->nextblockoff < flist->nblocks_at_last_alloc &&
				flist->nextblockoff > 0)
			{
				int numexistingblocks = flist->nblocks_at_last_alloc - flist->nextblockoff;
				int numextrablockstoadd;
				int nremainingdelpages = vstate->npendingpages - freeblockn;

				if (nremainingdelpages == 0)
					break;

				numextrablockstoadd = FSM_MAX_SPACE_FOR_BLOCKS_PER_FREELIST -
									  numexistingblocks;
				numextrablockstoadd = Min(numextrablockstoadd, nremainingdelpages);

				if (numextrablockstoadd == 0)
					break;

				memmove(&flist->blocks[0], &flist->blocks[flist->nextblockoff],
						sizeof(BlockNumber) * numexistingblocks);
				flist->nextblockoff = 0; /* This is actually the same next block logically */
				flist->ownerxid = FirstNormalFullTransactionId;
				flist->nblocks_at_last_alloc = FSM_MAX_SPACE_FOR_BLOCKS_PER_FREELIST;
				flist->nrefreshes++;

				for (int j = 0; j < numextrablockstoadd; j++)
				{
					FSMFreeBlock newblock;

					if (vstate->npendingpages == freeblockn)
						break;

					newblock.blk = vstate->pendingpages[freeblockn++].target;
					newblock.deleted = true;
					flist->blocks[numexistingblocks + j] = newblock;

					/* Do accounting */
					flist->ndelblocks++;
					flist->nconsumedblocks--;
					Assert(flist->ndelblocks >= 0);
					Assert(flist->nconsumedblocks >= 0);
				}
			}
		}
	}

	if (vstate->npendingpages > freeblockn)
		elog(WARNING, "index %s leaking %d out of %d deleted pages due to lack of shared mem",
			 RelationGetRelationName(rel), vstate->npendingpages - freeblockn,
			 vstate->npendingpages);

	LWLockRelease(FSMListLock);
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

/*
 * Debugging aid
 *
 * Doesn't actually report blocks allocated.  Actually reports blocks consumed
 * so far.  This is just an easy way of keeping track of what's really going
 * on during high level testing; SQL can call pg_relation_size(oid, 'fsm') to
 * see the number of consumed blocks.
 *
 * Note: FSMGetRelation() will pretend that the first list had all blocks ever
 * allocated and consumed when we reset list for a relation (e.g., following
 * server restart).
 *
 * XXX: This relies on the assumption that the number of freelists for a rel
 * can only ever increase.  Though we still do allow nblocks_at_last_alloc to
 * go up and down as conditions dictate.
 */
int64
FreeSpaceMapRelationGetNumberOfBlocks(RelFileNode rfn)
{

	FSMRelation key;
	FSMRelation *fsmrel;
	int64		totalnblocksalloced = 0;
	int64		totalnconsumedblocks = 0;
	int64		totalndelblocks = 0;

	memset(&key.key, 0, sizeof(FSMRelationHashKey));
	key.key.relfilenode = rfn;

	LWLockAcquire(FSMListLock, LW_SHARED);

	/* Find or create an entry with desired hash code */
	fsmrel = (FSMRelation *) hash_search(fsm_hash, &key.key, HASH_FIND, NULL);
	if (fsmrel)
	{
		for (int i = 0; i < fsmrel->nfreelists; i++)
		{
			FSMFreeList *flist = fsmrel->freelists + i;

			totalnblocksalloced += flist->nblocksalloced;
			totalnconsumedblocks += flist->nconsumedblocks;
			totalndelblocks += flist->ndelblocks;
		}
	}

	LWLockRelease(FSMListLock);


	Assert(totalnconsumedblocks <= totalnblocksalloced);
	Assert(totalnconsumedblocks >= totalndelblocks);

	/*
	 * The number of consumed blocks can go down when we delete a page -- it
	 * is effectively unconsumed.  Account for this here -- show caller the
	 * total number of blocks that have been written to at least once.
	 *
	 * We only leave out sparsely allocated blocks not yet consumed (much less
	 * deleted) even once.
	 */
	return totalnconsumedblocks + totalndelblocks;
}

static FSMRelation *
FSMGetRelation(Relation rel, bool reset)
{
	FSMRelation key;
	FSMRelation *fsmrel;
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
	fsmrel = (FSMRelation *) hash_search(fsm_hash, &key.key, HASH_ENTER,
										 &found);

	if (!found || reset)
	{
		/* New entry, initialize it */
		fsmrel->nfreelists = 1;
		fsmrel->relnumextensionops = 0;
		fsmrel->relnumvacuumops = 0;
		fsmrel->rellastleaderxid = InvalidFullTransactionId;
		fsmrel->dbgtotalnblocksalloced = -1;

		/*
		 * We track total relation size in shared memory
		 *
		 * FIXME: relnblocks field doesn't yet reliably agree with
		 * RelationGetNumberOfBlocks().
		 */
		fsmrel->relnblocks = RelationGetNumberOfBlocks(rel);

		/*
		 * For now just say that all alloc'd blocks were allocated using
		 * first and only freelist -- keep debugging stuff happy this way
		 */
		fsmrel->freelists[0].nblocks_at_last_alloc = 0;
		fsmrel->freelists[0].nextblockoff = 0;
		fsmrel->freelists[0].nblocksalloced = fsmrel->relnblocks;
		fsmrel->freelists[0].ndelblocks = 0;
		fsmrel->freelists[0].nconsumedblocks = fsmrel->relnblocks;
	}
#ifdef USE_ASSERT_CHECKING
	else if (!RelationUsesLocalBuffers(rel) &&
			 (rel->rd_rel->relam == HEAP_TABLE_AM_OID ||
			  rel->rd_rel->relam == BTREE_AM_OID))
	{
		int64		totalnblocksalloced = 0;
		int64		totalnconsumedblocks = 0;
		int64		totalnblocksavailnow = 0;
		int64		nblocks = RelationGetNumberOfBlocks(rel);

		/* Assert(relfreelists->nfreelists >= 1); */
		Assert(fsmrel->nfreelists <= FSM_MAX_FREELISTS_PER_RELATION);

		for (int i = 0; i < fsmrel->nfreelists; i++)
		{
			FSMFreeList *flist = fsmrel->freelists + i;

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksalloced  += flist->nblocksalloced;
			totalnconsumedblocks += flist->nconsumedblocks;
			totalnblocksavailnow += flist->nblocks_at_last_alloc - flist->nextblockoff;
		}

		/*
		 * Pageinspect tests don't look at output, but try to catch egregious
		 * regressions here:
		 */
		if (nblocks != totalnblocksalloced &&
			totalnblocksalloced != fsmrel->dbgtotalnblocksalloced)
		{
			elog(WARNING, "%s leaked %lu (authoritative nblocks: %lu, totalnblocksalloced: %lu)",
				 RelationGetRelationName(rel), nblocks - totalnblocksalloced,
				 nblocks, totalnblocksalloced);

			fsmrel->dbgtotalnblocksalloced = totalnblocksalloced;	/* Just complain once */
		}
	}
#endif

	return fsmrel;
}

static BlockNumber
FSMRelationReset(Relation rel, BlockNumber nblocks)
{
	FSMRelation *fsmrel;

	LWLockAcquire(FSMListLock, LW_EXCLUSIVE);
	fsmrel = FSMGetRelation(rel, true);
	if (fsmrel)
	{
		fsmrel->relnblocks = nblocks;
		fsmrel->nfreelists = 1;
		/* XXX Change nblocks_at_last_alloc here? */
		fsmrel->freelists[0].nextblockoff = fsmrel->freelists[0].nblocks_at_last_alloc;
		fsmrel->freelists[0].nblocksalloced = nblocks;
		fsmrel->freelists[0].ndelblocks = 0;
		fsmrel->freelists[0].nconsumedblocks = nblocks;
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
	FSMRelation key;
	FSMRelation *fsmrel;
	int64		totalnblocksalloced = 0;
	int64		totalnconsumedblocks = 0;
	int64		totalnblocksavailnow = 0;
	int64		nblocks;

	memset(&key.key, 0, sizeof(FSMRelationHashKey));
	key.key.relfilenode = rel->rd_node;

	initStringInfo(sinfo);

	LWLockAcquire(FSMListLock, LW_SHARED);

	nblocks = RelationGetNumberOfBlocks(rel);

	/* Find or create an entry with desired hash code */
	fsmrel = (FSMRelation *) hash_search(fsm_hash, &key.key, HASH_FIND, NULL);
	if (fsmrel)
	{
		RelFileNode relfilenode = fsmrel->key.relfilenode;

		appendStringInfo(sinfo, "%u/%u/%u:\n\n",
						 relfilenode.spcNode, relfilenode.dbNode,
						 relfilenode.relNode);

		for (int i = 0; i < fsmrel->nfreelists; i++)
		{
			FSMFreeList *flist = fsmrel->freelists + i;

			appendStringInfo(sinfo, "    %d - nextblockoff: %d[blk %d]/%d, ownerxid: %u:%u, nconsumedblocks: %ld, nrefreshes: %ld, lastLeader: %u:%u\n",
							 i, flist->nextblockoff,
							 (flist->nextblockoff == flist->nblocks_at_last_alloc ? -1 : flist->blocks[flist->nextblockoff].blk),
							 flist->nblocks_at_last_alloc,
							 EpochFromFullTransactionId(flist->ownerxid),
							 XidFromFullTransactionId(flist->ownerxid),
							 flist->nconsumedblocks, flist->nrefreshes,
							 EpochFromFullTransactionId(flist->leaderxid),
							 XidFromFullTransactionId(flist->leaderxid));

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksalloced += flist->nblocksalloced;
			totalnconsumedblocks += flist->nconsumedblocks;
			totalnblocksavailnow += flist->nblocks_at_last_alloc - flist->nextblockoff;
		}

		appendStringInfo(sinfo, "  totalnconsumedblocks: %lu, totalnblocksavailnow: %lu, totalnblocksalloced : %lu, RelationGetNumberOfBlocks(): %lu\n",
						 totalnconsumedblocks, totalnblocksavailnow,
						 totalnblocksalloced, nblocks);
		appendStringInfo(sinfo, "rellastleaderxid: %u:%u, relnumextensionops: %lu, fsmrel->relnumvacuumops: %lu\n",
						 EpochFromFullTransactionId(fsmrel->rellastleaderxid),
						 XidFromFullTransactionId(fsmrel->rellastleaderxid),
						 fsmrel->relnumextensionops,
						 fsmrel->relnumvacuumops);
		appendStringInfo(sinfo, "RelationGetNumberOfBlocks(): %lu, fsmrel->relnblocks: %u (these should match)\n",
						 nblocks, fsmrel->relnblocks);

		/*
		 * Pageinspect tests don't look at output, but try to catch egregious
		 * regressions here:
		 */
		if (nblocks != fsmrel->relnblocks)
			elog(WARNING, "leaked %lu", nblocks - fsmrel->relnblocks);
	}

	LWLockRelease(FSMListLock);
}

void
DebugFreeSpaceMapDumpAllRels(StringInfo sinfo)
{
	HASH_SEQ_STATUS hash_seq;
	FSMRelation *fsmrel = NULL;
	int			n = 0;

	initStringInfo(sinfo);

	LWLockAcquire(FSMListLock, LW_SHARED);

	Assert(fsm_hash != NULL);

	hash_seq_init(&hash_seq, fsm_hash);
	while ((fsmrel = hash_seq_search(&hash_seq)) != NULL)
	{
		Relation rel = NULL;
		RelFileNode relfilenode = fsmrel->key.relfilenode;
		Oid			reloid;
		int64		totalnblocksalloced = 0;
		int64		totalnconsumedblocks = 0;
		int64		totalnblocksavailnow = 0;

		reloid = RelidByRelfilenode(relfilenode.spcNode, relfilenode.relNode);
		if (OidIsValid(reloid))
			rel = try_relation_open(reloid, AccessShareLock);

		if (!rel)
		{
			appendStringInfo(sinfo, "rel # %d (unknown) %u/%u/%u has %d freelists:\n",
							 n++, relfilenode.spcNode, relfilenode.dbNode,
							 relfilenode.relNode,
							 fsmrel->nfreelists);
		}
		else
		{
			appendStringInfo(sinfo, "rel # %d (\"%s\"): %u/%u/%u has %d freelists:\n",
							 n++, RelationGetRelationName(rel),
							 relfilenode.spcNode, relfilenode.dbNode,
							 relfilenode.relNode,
							 fsmrel->nfreelists);
		}

		for (int i = 0; i < fsmrel->nfreelists; i++)
		{
			FSMFreeList *flist = fsmrel->freelists + i;

			appendStringInfo(sinfo, "    %d - nextblockoff: %d[blk %d]/%d, ownerxid: %u:%u, nconsumedblocks: %ld, nrefreshes: %ld, lastLeader: %u:%u\n",
							 i, flist->nextblockoff,
							 (flist->nextblockoff == flist->nblocks_at_last_alloc ? -1 : flist->blocks[flist->nextblockoff].blk), flist->nblocks_at_last_alloc,
							 EpochFromFullTransactionId(flist->ownerxid),
							 XidFromFullTransactionId(flist->ownerxid),
							 flist->nconsumedblocks, flist->nrefreshes,
							 EpochFromFullTransactionId(flist->leaderxid),
							 XidFromFullTransactionId(flist->leaderxid));

			/* Tally # of all blocks every consumed from all lists */
			totalnblocksalloced  += flist->nblocksalloced;
			totalnconsumedblocks += flist->nconsumedblocks;
			totalnblocksavailnow += flist->nblocks_at_last_alloc - flist->nextblockoff;
		}

		if (!rel)
			appendStringInfo(sinfo, "  totalnblocksalloced: %lu, totalnconsumedblocks: %lu, totalnblocksavailnow: %lu\n",
							 totalnblocksalloced, totalnconsumedblocks,
							 totalnblocksavailnow);
		else
		{
			int64		nblocks = RelationGetNumberOfBlocks(rel);

			appendStringInfo(sinfo, "  totalnblocksalloced: %lu, totalnconsumedblocks: %lu, totalnblocksavailnow: %lu (relationgetnumberofblocks()-wise leaked: %lu)\n",
							 totalnblocksalloced, totalnconsumedblocks,
							 totalnblocksavailnow, nblocks - totalnblocksalloced);
			if (nblocks != totalnblocksalloced)
				elog(WARNING, "leaked %lu from rel \"%s\"", nblocks - totalnblocksalloced, RelationGetRelationName(rel));
		}
		appendStringInfo(sinfo, "  rellastleaderxid: %u:%u, relnumextensionops: %lu, relnumvacuumops: %lu\n\n",
						 EpochFromFullTransactionId(fsmrel->rellastleaderxid),
						 XidFromFullTransactionId(fsmrel->rellastleaderxid),
						 fsmrel->relnumextensionops,
						 fsmrel->relnumvacuumops);

		if (rel)
			relation_close(rel, AccessShareLock);
	}

	LWLockRelease(FSMListLock);
}
