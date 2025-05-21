/*-------------------------------------------------------------------------
 *
 * mdampath.h
 *	  MDAM (Multi-Dimensional Access Method) OR-clause optimization for
 *	  B-tree index scans.
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/optimizer/mdampath.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef MDAMPATH_H
#define MDAMPATH_H

#include "nodes/pathnodes.h"

extern void generate_mdam_or_paths(PlannerInfo *root, RelOptInfo *rel);

#endif							/* MDAMPATH_H */
