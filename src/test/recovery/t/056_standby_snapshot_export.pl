# Copyright (c) 2026, PostgreSQL Global Development Group
#
# Test snapshot export and import on a standby.
#
# A snapshot taken during recovery keeps its whole in-progress set, including
# every running top-level XID, in subxip, and leaves xip empty.  Exporting it
# must write subxip out even once the snapshot is marked suboverflowed, which
# happens as soon as a transaction on the primary reports 64 subtransactions.
# A buggy importer that loses subxip believes nothing at all is running between
# xmin and xmax, treats live transactions as aborted, and stamps
# HEAP_XMIN_INVALID/HEAP_XMAX_INVALID on their tuples.  Every other session
# on the standby then sees those hint bits.

use strict;
use warnings FATAL => 'all';
use PostgreSQL::Test::Cluster;
use PostgreSQL::Test::Utils;
use Test::More;

# Enough to arm lastOverflowedXid on the standby (which happens at 64) and to
# overflow the primary's own subxid cache (at 65), so that later
# xl_running_xacts records keep it armed.
my $nsubxacts = 80;

my $primary = PostgreSQL::Test::Cluster->new('primary');
$primary->init(allows_streaming => 1);
$primary->append_conf('postgresql.conf', 'autovacuum = off');
$primary->start;

$primary->backup('backup');
my $standby = PostgreSQL::Test::Cluster->new('standby');
$standby->init_from_backup($primary, 'backup', has_streaming => 1);
$standby->append_conf('postgresql.conf', 'max_standby_streaming_delay = -1');
$standby->start;

# visibility_test holds the rows whose hint bits the test checks.  xid_burner
# exists only to consume XIDs and subxids.
$primary->safe_psql(
	'postgres', q[
CREATE TABLE visibility_test(k int, pad text);
INSERT INTO visibility_test
  SELECT g, repeat('x', 1200) FROM generate_series(1, 40) g;
CREATE TABLE xid_burner(i int);
]);

# Hold the primary's removal horizon below the deleting transaction, so that
# the row version it deletes stays RECENTLY_DEAD.  Otherwise the re-INSERT
# prunes that version on the primary, and replay of the prune record removes
# the evidence from the standby.
my $horizon_holder = $primary->background_psql('postgres');
$horizon_holder->query_safe(
	'BEGIN ISOLATION LEVEL REPEATABLE READ;
	 SELECT count(*) FROM visibility_test');

# This transaction deletes a row and stays open, so every snapshot taken from
# here on must report its XID as running.
my $toplevel_deleter = $primary->background_psql('postgres');
$toplevel_deleter->query_safe('BEGIN');
$toplevel_deleter->query_safe('DELETE FROM visibility_test WHERE k = 7');
my $deleter_xid =
  $toplevel_deleter->query_safe('SELECT pg_current_xact_id()');

# This transaction deletes another row in an early subtransaction, then
# overflows its subxid cache and stays open.  Recovery removes the deleting
# subtransaction's XID from KnownAssignedXids, so checking that tuple's xmax
# later has to map the child XID back to its parent through pg_subtrans.
# Keeping the transaction open also keeps lastOverflowedXid armed on the
# standby.
my $subxact_deleter = $primary->background_psql('postgres');
$subxact_deleter->query_safe('BEGIN');
$subxact_deleter->query_safe('SAVEPOINT early');
$subxact_deleter->query_safe('DELETE FROM visibility_test WHERE k = 8');
$subxact_deleter->query_safe('RELEASE early');
$subxact_deleter->query_safe(
	qq[DO \$\$ BEGIN
	     FOR i IN 1..$nsubxacts LOOP
	       BEGIN INSERT INTO xid_burner VALUES (i);
	       EXCEPTION WHEN OTHERS THEN NULL; END;
	     END LOOP; END \$\$]);

# Commit something so that the standby's xmax ends up past the deleting XID,
# and to flush the xid-assignment WAL that those subtransactions wrote.  Then
# wait until the standby has removed the subxids and marks snapshots
# overflowed.  Without the extra commit, XidInMVCCSnapshot() answers through
# its xid >= xmax range test, and the test would pass without examining the
# in-progress array.
$primary->safe_psql('postgres', 'INSERT INTO xid_burner VALUES (0)');
$primary->wait_for_replay_catchup($standby);

my $exporter = $standby->background_psql('postgres');
$exporter->query_safe('BEGIN ISOLATION LEVEL REPEATABLE READ');
my $recovery_snap = $exporter->query_safe('SELECT pg_export_snapshot()');

my $recovery_file =
  slurp_file($standby->data_dir . "/pg_snapshots/$recovery_snap");
note("exported snapshot $recovery_snap:\n$recovery_file");

like($recovery_file, qr/^rec:1$/m, 'snapshot was taken during recovery');
like($recovery_file, qr/^sof:1$/m, 'snapshot is suboverflowed');

# Without these the test could pass while exercising nothing: an xmax below
# the deleting XID answers "not running" through the plain range test instead.
my ($exported_xmin) = $recovery_file =~ /^xmin:(\d+)$/m;
my ($exported_xmax) = $recovery_file =~ /^xmax:(\d+)$/m;
cmp_ok($exported_xmin, '<=', $deleter_xid,
	'exported xmin does not follow the running XID');
cmp_ok($deleter_xid, '<', $exported_xmax,
	'running XID precedes exported xmax');

like($recovery_file, qr/^sxcnt:[1-9]/m,
	'suboverflowed recovery snapshot exports its subxip array');
like($recovery_file, qr/^sxp:$deleter_xid$/m, 'running XID is exported');

# Import the snapshot and read the table.  This answers correctly (a
# still-running transaction and an aborted one are indistinguishable here),
# but it is what writes the hint bits.
my $importer = $standby->background_psql('postgres');
$importer->query_safe(
	qq[BEGIN ISOLATION LEVEL REPEATABLE READ;
	   SET TRANSACTION SNAPSHOT '$recovery_snap']);
is($importer->query_safe('SELECT count(*) FROM visibility_test'),
	40, 'importing backend sees its own snapshot');
$importer->query_safe('COMMIT');
$exporter->query_safe('COMMIT');

# The deleting transaction commits, and the freed key is used again.
$toplevel_deleter->query_safe('COMMIT');
$primary->safe_psql('postgres',
	q[INSERT INTO visibility_test VALUES (7, repeat('y', 1200))]);
$primary->wait_for_replay_catchup($standby);

# Sessions that never touched the exported snapshot must agree with the
# primary.  A stale HEAP_XMAX_INVALID on the deleted row version brings it
# back to life, so the key is returned twice.
is( $standby->safe_psql(
		'postgres', 'SELECT count(*) FROM visibility_test WHERE k = 7'),
	1,
	'standby sees the re-inserted row once');

my $digest =
  q[SELECT md5(string_agg(k::text, ',' ORDER BY k)) FROM visibility_test];
is( $standby->safe_psql('postgres', $digest),
	$primary->safe_psql('postgres', $digest),
	'primary and standby agree on table contents');

# pg_snapshots is only emptied at startup, and ImportSnapshot() takes rec:
# from the file rather than from RecoveryInProgress().  A snapshot exported
# by a standby therefore outlives promotion, and keeps taking
# XidInMVCCSnapshot()'s recovery branch on a server no longer in recovery.
my $promotion_exporter = $standby->background_psql('postgres');
$promotion_exporter->query_safe('BEGIN ISOLATION LEVEL REPEATABLE READ');
my $promotion_snap =
  $promotion_exporter->query_safe('SELECT pg_export_snapshot()');
my $promotion_count =
  $promotion_exporter->query_safe('SELECT count(*) FROM visibility_test');

like(slurp_file($standby->data_dir . "/pg_snapshots/$promotion_snap"),
	qr/^sof:1$/m,
	'snapshot saved for post-promotion re-export is suboverflowed');

# The deleting child XID is no longer in KnownAssignedXids, so this needs
# pg_subtrans to reach its parent.
$subxact_deleter->query_safe('COMMIT');
$primary->wait_for_replay_catchup($standby);
is( $standby->safe_psql(
		'postgres', 'SELECT count(*) FROM visibility_test WHERE k = 8'),
	0,
	'standby sees the committed subtransaction delete');

$standby->promote;
$standby->poll_query_until('postgres', 'SELECT NOT pg_is_in_recovery()')
  or die "standby never finished promotion";

my $reexporter = $standby->background_psql('postgres');
$reexporter->query_safe(
	qq[BEGIN ISOLATION LEVEL REPEATABLE READ;
	   SET TRANSACTION SNAPSHOT '$promotion_snap']);
is($reexporter->query_safe('SELECT count(*) FROM visibility_test'),
	$promotion_count,
	'recovery-taken snapshot still imports after promotion');

# That importing transaction is read-write, since the read-only requirement
# binds only a SERIALIZABLE source.  So it can acquire subcommitted children
# and export again, which hands ExportSnapshot() a snapshot taken during
# recovery together with a non-empty children array.  Those children are at
# or above the old xmax, and must not reach the file.
$reexporter->query_safe('SAVEPOINT sp');
$reexporter->query_safe(
	q[INSERT INTO visibility_test VALUES (5000, repeat('z', 10))]);
$reexporter->query_safe('RELEASE sp');
my $reexported_snap = $reexporter->query_safe('SELECT pg_export_snapshot()');

my $reexported_file =
  slurp_file($standby->data_dir . "/pg_snapshots/$reexported_snap");
my ($reexported_xmin) = $reexported_file =~ /^xmin:(\d+)$/m;
my ($reexported_xmax) = $reexported_file =~ /^xmax:(\d+)$/m;
my @reexported_subxids = $reexported_file =~ /^sxp:(\d+)$/mg;
like($reexported_file, qr/^sof:1$/m,
	're-exported recovery snapshot remains suboverflowed');
like($reexported_file, qr/^sxcnt:[1-9]/m,
	'a recovery-taken snapshot re-exports its subxip array after a write');
is( scalar(
		grep { $_ < $reexported_xmin || $_ >= $reexported_xmax }
		  @reexported_subxids),
	0,
	're-exported snapshot omits XIDs outside its range');

my $reimporter = $standby->background_psql('postgres');
$reimporter->query_safe(
	qq[BEGIN ISOLATION LEVEL REPEATABLE READ;
	   SET TRANSACTION SNAPSHOT '$reexported_snap']);
is($reimporter->query_safe('SELECT count(*) FROM visibility_test'),
	$promotion_count, 're-exported recovery snapshot can be imported');

$reimporter->query_safe('COMMIT');
$reexporter->query_safe('COMMIT');
$promotion_exporter->query_safe('COMMIT');

$horizon_holder->quit;
$toplevel_deleter->quit;
$subxact_deleter->quit;
$exporter->quit;
$importer->quit;
$promotion_exporter->quit;
$reexporter->quit;
$reimporter->quit;
$standby->stop;
$primary->stop;

done_testing();
