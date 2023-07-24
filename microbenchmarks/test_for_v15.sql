-- based on https://postgr.es/m/527571eb98b9bed54c8cadb261c14795@oss.nttdata.com

\pset pager off
set client_min_messages='notice';
set enable_seqscan=off;
DROP TABLE IF EXISTS t;
CREATE unlogged TABLE t (id1 int, id2 int);
INSERT INTO t (SELECT i, i FROM generate_series(1,1_000_000) s(i));
CREATE INDEX t_idx on t (id1, id2);
VACUUM FREEZE ANALYZE;

\echo 'PATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id2 = 1 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id2 = 1;
SELECT * FROM t WHERE id2 = 1;
SELECT * FROM t WHERE id2 = 1;
\echo 'Should be ~17ms ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id2 = 1 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id2 = 1;
SELECT * FROM t WHERE id2 = 1;
SELECT * FROM t WHERE id2 = 1;
\echo 'Should be ~17ms ^^^'

\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id2 = 1_000_000 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id2 = 1_000_000;
SELECT * FROM t WHERE id2 = 1_000_000;
SELECT * FROM t WHERE id2 = 1_000_000;
\echo 'Should be ~16.5ms ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id2 = 1_000_000 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id2 = 1_000_000;
SELECT * FROM t WHERE id2 = 1_000_000;
SELECT * FROM t WHERE id2 = 1_000_000;
\echo 'Should be ~16.5ms ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
\echo 'Master is ~25ms, but patch can get it down to ~17ms ^^^'

\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1;
\echo 'Master is ~25ms, but patch can get it down to ~17ms ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
\echo 'Master is ~25ms, but patch can get it down to ~17ms here too ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 = 1_000_000;
\echo 'Master is ~25ms, but patch can get it down to ~17ms here too ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000) query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
\echo 'Master is ~44ms for this simple SAOP query, which patch now seems to match ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000) query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id2 IN (0, 1, 1_000_000);
\echo 'Master is ~44ms for this simple SAOP query, which patch now seems to match ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000) query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
\echo 'Master is ~53ms for this range + SAOP query, but patch can get it down to ~45ms here ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000) query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 IN (0, 1, 1_000_000);
\echo 'Master is ~53ms for this range + SAOP query, but patch can get it down to ~45ms here ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
\echo 'Master is ~26ms, patch lowers that to ~17ms, since there is only a single range skip array involved ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 <= 1;
\echo 'Master is ~26ms, patch lowers that to ~17ms, since there is only a single range skip array involved ^^^'


\echo '\n\nPATCH (skipscan_prefix_cols=32), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3 query:'
SET skipscan_prefix_cols=32;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
\echo 'Master is ~34ms with range skip array + separate inequalities, but patch can get it down to ~25ms ^^^'


\echo '\n\nMASTER (skipscan_prefix_cols=0), SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3 query:'
SET skipscan_prefix_cols=0;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
SELECT * FROM t WHERE id1 BETWEEN 0 AND 1_000_000 AND id2 between 1 AND 3;
\echo 'Master is ~34ms with range skip array + separate inequalities, but patch can get it down to ~25ms ^^^'
