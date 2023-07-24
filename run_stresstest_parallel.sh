#!/bin/zsh
for ((;;)) PSQL_PAGER= p -f microbenchmarks/parallel_index_scan_testcase_medium.sql && sleep 0.1
