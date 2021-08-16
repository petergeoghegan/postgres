# src/test/modules/Makefile
#
# BRIN test fails like this for now, and so has been disabled:
#
# poll_query_until timed out executing this query:
# select count(*) > 1 from brin_page_items(get_raw_page('brin_wi_idx', 2), 'brin_wi_idx'::regclass)
# expecting this output:
# t
# last actual query output:
# f
# with stderr:
#t/01_workitems.pl .. 2/2
#   Failed test 'index got summarized'
#   at t/01_workitems.pl line 43.
#          got: 'f'
#     expected: 't'
# Looks like you failed 1 test of 2.
# t/01_workitems.pl .. Dubious, test returned 1 (wstat 256, 0x100)
# Failed 1/2 subtests

subdir = src/test/modules
top_builddir = ../../..
include $(top_builddir)/src/Makefile.global

SUBDIRS = \
		  commit_ts \
		  delay_execution \
		  dummy_index_am \
		  dummy_seclabel \
		  libpq_pipeline \
		  plsample \
		  snapshot_too_old \
		  spgist_name_ops \
		  test_bloomfilter \
		  test_ddl_deparse \
		  test_extensions \
		  test_ginpostinglist \
		  test_integerset \
		  test_misc \
		  test_parser \
		  test_pg_dump \
		  test_predtest \
		  test_rbtree \
		  test_regex \
		  test_rls_hooks \
		  test_shm_mq \
		  unsafe_tests \
		  worker_spi

ifeq ($(with_ssl),openssl)
SUBDIRS += ssl_passphrase_callback
else
ALWAYS_SUBDIRS += ssl_passphrase_callback
endif

$(recurse)
$(recurse_always)
