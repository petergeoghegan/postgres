/*-------------------------------------------------------------------------
 *
 * freespace.h
 *	  POSTGRES free space map for quickly finding free space in relations
 *
 *
 * Portions Copyright (c) 1996-2021, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/storage/freespace.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef FREESPACE_H_
#define FREESPACE_H_

#include "access/heapam.h"
#include "access/nbtree.h"
#include "storage/block.h"
#include "storage/relfilenode.h"
#include "utils/relcache.h"

/* prototypes for public functions in freespace.c */
extern Size GetRecordedFreeSpace(Relation rel, BlockNumber heapBlk);
extern BlockNumber GetPageWithFreeSpace(Relation rel, Size spaceNeeded,
										BulkInsertState bistate);
extern BlockNumber BTreeGetIndexPageWithFreeSpace(Relation rel);
extern Buffer FreeSpaceMapAddExtraBlocks(Relation rel,
										 Size spaceNeeded,
										 BulkInsertState bistate);
extern void BTreeIndexFreeSpaceMapVacuum(Relation rel, BTVacState *vstate);
extern BlockNumber RecordAndGetPageWithFreeSpace(Relation rel,
												 BlockNumber oldPage,
												 Size oldSpaceAvail,
												 Size spaceNeeded);
extern void RecordPageWithFreeSpace(Relation rel, BlockNumber heapBlk,
									Size spaceAvail);
extern void XLogRecordPageWithFreeSpace(RelFileNode rnode, BlockNumber heapBlk,
										Size spaceAvail);

extern BlockNumber FreeSpaceMapPrepareTruncateRel(Relation rel,
												  BlockNumber nblocks);
extern void FreeSpaceMapVacuum(Relation rel);

extern void FreeSpaceMapVacuumRange(Relation rel, BlockNumber start,
									BlockNumber end);
extern Size FreeSpaceMapShmemSize(void);
extern void FreeSpaceMapShmemInit(void);
extern int64 FreeSpaceMapRelationGetNumberOfBlocks(RelFileNode rfn);
extern void DebugFreeSpaceMapDump(Relation rel, StringInfo sinfo);
extern void DebugFreeSpaceMapDumpAllRels(StringInfo sinfo);

#endif							/* FREESPACE_H_ */
