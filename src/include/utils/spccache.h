/*-------------------------------------------------------------------------
 *
 * spccache.h
 *	  Tablespace cache.
 *
 * Portions Copyright (c) 1996-2026, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * src/include/utils/spccache.h
 *
 *-------------------------------------------------------------------------
 */
#ifndef SPCCACHE_H
#define SPCCACHE_H

extern void get_tablespace_page_costs(Oid spcid, double *spc_random_page_cost,
									  double *spc_seq_page_cost,
									  int *spc_io_concurrency);
extern int	get_tablespace_io_concurrency(Oid spcid);
extern int	get_tablespace_maintenance_io_concurrency(Oid spcid);

#endif							/* SPCCACHE_H */
