# Copyright (c) 2026, PostgreSQL Global Development Group
#
# Test snapshot export and import on a standby.
#
# A snapshot taken during recovery stores every running XID -- top-level ones
# included -- in subxip, leaving xip empty.  Once such a snapshot is also
# marked suboverflowed, which happens as soon as some transaction on the
# primary reports 64 subtransactions, exporting it must still write subxip
# out: an importer that loses it believes nothing at all is running between
# xmin and xmax, treats live transactions as aborted, and stamps
# HEAP_XMIN_INVALID / HEAP_XMAX_INVALID on their tuples.  Those hint bits are
# then seen by every other session on the standby.

use strict;
use warnings FATAL => 'all';
use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

# Enough XID-acquiring subtransactions to arm lastOverflowedXid on the
# standby, which happens at 64, and to overflow the primary's own subxid
# cache at 65 so that later xl_running_xacts records keep it armed.
my $nsubxacts = 80;

# Checksums are off on purpose.  With XLogHintBitIsNeeded(),
# MarkSharedBufferDirtyHint() declines to dirty a page for a hint bit set
# during recovery, so a wrong hint would live in shared buffers only.
my $primary = PostgreSQL::Test::Cluster->new('primary');
$primary->init(allows_streaming => 1, no_data_checksums => 1);
$primary->append_conf(
	'postgresql.conf', q[
autovacuum = off
checkpoint_timeout = 1h
max_wal_size = 10GB
]);
$primary->start;

$primary->backup('backup');
my $standby = PostgreSQL::Test::Cluster->new('standby');
$standby->init_from_backup($primary, 'backup', has_streaming => 1);
$standby->append_conf(
	'postgresql.conf', q[
hot_standby_feedback = off
max_standby_streaming_delay = -1
]);
$standby->start;

$primary->safe_psql(
	'postgres', q[
CREATE TABLE victim(k int PRIMARY KEY, pad text);
INSERT INTO victim SELECT g, repeat('x', 1200) FROM generate_series(1, 40) g;
CREATE TABLE burner(i int);
]);

# Hold the primary's removal horizon below U so that the version U deletes
# stays RECENTLY_DEAD.  Otherwise the re-INSERT prunes it on the primary and
# replay of the prune record removes the evidence from the standby.
my $guard = $primary->background_psql('postgres');
$guard->query_safe(
	'BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT count(*) FROM victim');

# U deletes a row and stays open, so every snapshot taken from here on must
# report its XID as running.
my $u = $primary->background_psql('postgres');
$u->query_safe('BEGIN');
$u->query_safe('DELETE FROM victim WHERE k = 7');
my $u_xid = $u->query_safe('SELECT pg_current_xact_id()');

# O overflows the subxid cache and stays open, keeping lastOverflowedXid
# armed on the standby for as long as it lives.
my $o = $primary->background_psql('postgres');
$o->query_safe('BEGIN');
$o->query_safe(
	qq[DO \$\$ BEGIN
	     FOR i IN 1..$nsubxacts LOOP
	       BEGIN INSERT INTO burner VALUES (i); EXCEPTION WHEN OTHERS THEN NULL; END;
	     END LOOP; END \$\$]);

# Commit something so that the standby's xmax ends up past U's XID.  Without
# this, XidInMVCCSnapshot() answers via its xid >= xmax range test and the
# test would pass without exercising anything.
$primary->safe_psql('postgres', 'INSERT INTO burner VALUES (0)');
$primary->wait_for_replay_catchup($standby);

# pg_current_snapshot() reads the same pair of arrays and has to make the same
# distinction.  Reporting a running transaction as visible would contradict
# what the very snapshot it came from shows.  Unlike the export path this
# needs no overflow: on a standby xip is always empty.
is($standby->safe_psql(
		'postgres',
		"SELECT pg_visible_in_snapshot('$u_xid'::xid8, pg_current_snapshot())"),
	'f', 'pg_visible_in_snapshot agrees a running XID is not visible');

is($standby->safe_psql('postgres', 'SELECT count(*) FROM victim WHERE k = 7'),
	1, 'and its delete has not taken effect');

my $s1 = $standby->background_psql('postgres');
$s1->query_safe('BEGIN ISOLATION LEVEL REPEATABLE READ');
my $snap = $s1->query_safe('SELECT pg_export_snapshot()');

my $file = slurp_file($standby->data_dir . "/pg_snapshots/$snap");
note("exported snapshot $snap:\n$file");

like($file, qr/^rec:1$/m, 'snapshot was taken during recovery');
like($file, qr/^sof:1$/m, 'snapshot is suboverflowed');

# Without these the test could pass while exercising nothing: an xmax below
# U's XID answers "not running" through the plain range test instead.
my ($xmin) = $file =~ /^xmin:(\d+)$/m;
my ($xmax) = $file =~ /^xmax:(\d+)$/m;
cmp_ok($xmin, '<=', $u_xid, 'exported xmin does not follow the running XID');
cmp_ok($u_xid, '<', $xmax, 'running XID precedes exported xmax');

like($file, qr/^sxcnt:[1-9]/m,
	'suboverflowed recovery snapshot exports its subxip array');
like($file, qr/^sxp:$u_xid$/m, 'running XID is exported');

# Import the snapshot and read the victim page.  This answers correctly --
# a transaction that is still running and one that aborted are
# indistinguishable here -- but it is what writes the hint bits.
my $s2 = $standby->background_psql('postgres');
$s2->query_safe(
	qq[BEGIN ISOLATION LEVEL REPEATABLE READ;
	   SET TRANSACTION SNAPSHOT '$snap';
	   SET enable_indexscan = off;
	   SET enable_bitmapscan = off;
	   SET enable_indexonlyscan = off]);
is($s2->query_safe('SELECT count(*) FROM victim'),
	40, 'importing backend sees its own snapshot');
$s2->query_safe('COMMIT');
$s1->query_safe('COMMIT');

# U commits and the freed key is used again.
$u->query_safe('COMMIT');
$primary->safe_psql('postgres',
	q[INSERT INTO victim VALUES (7, repeat('y', 1200))]);
$o->query_safe('COMMIT');
$primary->wait_for_replay_catchup($standby);

# Sessions that never touched the exported snapshot must agree with the
# primary.  A stale HEAP_XMAX_INVALID on the version U deleted brings the old
# row back to life, so the key is returned twice.
is($standby->safe_psql('postgres', 'SELECT count(*) FROM victim WHERE k = 7'),
	1, 'standby sees the re-inserted row once');

is( $standby->safe_psql(
		'postgres',
		'SELECT count(*) FROM (SELECT k FROM victim GROUP BY k HAVING count(*) > 1) d'
	),
	0, 'no duplicate keys on standby');

my $digest = q[SELECT md5(string_agg(k::text, ',' ORDER BY k)) FROM victim];
is($standby->safe_psql('postgres', $digest),
	$primary->safe_psql('postgres', $digest),
	'primary and standby agree on table contents');

# pg_snapshots is only emptied at startup and ImportSnapshot() takes rec: from
# the file rather than from RecoveryInProgress(), so a snapshot exported by a
# standby outlives promotion and keeps taking XidInMVCCSnapshot()'s recovery
# branch on a server that is no longer in recovery.
my $s3 = $standby->background_psql('postgres');
$s3->query_safe('BEGIN ISOLATION LEVEL REPEATABLE READ');
my $snap2 = $s3->query_safe('SELECT pg_export_snapshot()');
my $before = $s3->query_safe('SELECT count(*) FROM victim');

$standby->promote;
$standby->poll_query_until('postgres', 'SELECT NOT pg_is_in_recovery()')
  or die "standby never finished promotion";

my $s4 = $standby->background_psql('postgres');
$s4->query_safe(
	qq[BEGIN ISOLATION LEVEL REPEATABLE READ;
	   SET TRANSACTION SNAPSHOT '$snap2']);
is($s4->query_safe('SELECT count(*) FROM victim'),
	$before, 'recovery-taken snapshot still imports after promotion');

# That importing transaction is read-write, since the read-only requirement
# binds only a SERIALIZABLE source.  So it can acquire subcommitted children
# and export again, which hands ExportSnapshot() a snapshot taken during
# recovery together with a non-empty children array.
$s4->query_safe('SAVEPOINT sp');
$s4->query_safe(q[INSERT INTO victim VALUES (5000, repeat('z', 10))]);
$s4->query_safe('RELEASE sp');
my $snap3 = $s4->query_safe('SELECT pg_export_snapshot()');

my $file3 = slurp_file($standby->data_dir . "/pg_snapshots/$snap3");
like($file3, qr/^sxcnt:[1-9]/m,
	'a recovery-taken snapshot re-exports its subxip array after a write');

$s4->query_safe('COMMIT');
$s3->query_safe('COMMIT');

$guard->quit;
$u->quit;
$o->quit;
$s1->quit;
$s2->quit;
$s3->quit;
$s4->quit;
$standby->stop;
$primary->stop;

done_testing();
