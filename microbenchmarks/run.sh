#!/bin/bash

branch=$1
nrows=$2
dbname=regression
resultfile="$branch"
resultfile+="_results.csv"
# export PGPORT=5555

rm "$dbname.log"
rm "$resultfile"
rm debug.log
PSQL_PAGER=""

function query_duration() {
	scan=$1
	query=$2

	seqscan=off
	bitmapscan=off
	indexscan=off
	indexonlyscan=off

	if [ "$scan" == "seqscan" ]; then
		seqscan=on
	elif [ "$scan" == "bitmapscan" ]; then
		bitmapscan=on
	elif [ "$scan" == "indexscan" ]; then
		indexscan=on
	elif [ "$scan" == "indexonlyscan" ]; then
		indexscan=on
		indexonlyscan=on
	fi

	psql --no-psqlrc $dbname > $dbname.log 2>&1 <<EOF
SET enable_seqscan=$seqscan;
SET enable_bitmapscan=$bitmapscan;
SET enable_indexscan=$indexscan;
SET enable_indexonlyscan=$indexonlyscan;
SET max_parallel_workers_per_gather=0;
--select \$foo\$EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM ($query OFFSET 0) foo OFFSET 1000000000;\$foo\$;
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM ($query OFFSET 0) foo OFFSET 1000000000;
SELECT extract(epoch from now()), 'start';
SELECT * FROM ($query OFFSET 0) foo OFFSET 1000000000;
SELECT extract(epoch from now()), 'end';
EOF

	cat $dbname.log >> debug.log

	start=$(grep 'start' $dbname.log | awk '{print $1}')
	end=$(grep 'end' $dbname.log | awk '{print $1}')

	echo "($end - $start) * 1000" | bc
}

ndistinct=1

# while /bin/true; do
for ndistinct_prop in 1 5 100 1000; do

	ndistinct=$(($nrows / $ndistinct_prop))

	if [[ $ndistinct -gt $nrows ]]; then
	    echo Failed! >&2
		break
	fi

	for type in int ; do

		# for data in random sequential cycle correlated; do
		for data in sequential cycle correlated; do

      tablename="t"
      tablename+="$ndistinct_prop"
      tablename+="_"
      tablename+="$type"
      tablename+="_"
      tablename+="$data"
			echo "===== NDISTINCT $ndistinct TYPE $type DATA $data ====="

      psql $dbname -c "drop table if exists $tablename";

			psql $dbname -c "create unlogged table $tablename (v $type)";

			psql $dbname -c "create index on $tablename (v)";

			if [ "$data" == "random" ]; then
				echo  "insert into $tablename select $ndistinct * random() from generate_series(1, $nrows) s(i)"
        psql $dbname -c "select setseed(0.12345); insert into $tablename select $ndistinct * random() from generate_series(1, $nrows) s(i)"
			elif [ "$data" == "sequential" ]; then
				echo "insert into $tablename select i::float * $ndistinct / $nrows from generate_series(1, $nrows) s(i)"
				psql $dbname -c "select setseed(0.12345); insert into $tablename select i::float * $ndistinct / $nrows from generate_series(1, $nrows) s(i)"
			elif [ "$data" == "cycle" ]; then
				echo "insert into $tablename select mod(i,$ndistinct) from generate_series(1, $nrows) s(i)"
				psql $dbname -c "select setseed(0.12345); insert into $tablename select mod(i,$ndistinct) from generate_series(1, $nrows) s(i)"
			elif [ "$data" == "correlated" ]; then
				echo "insert into $tablename select i::float * $ndistinct / $nrows + random() * sqrt($ndistinct) from generate_series(1, $nrows) s(i)"
				psql $dbname -c "select setseed(0.12345); insert into $tablename select i::float * $ndistinct / $nrows + random() * sqrt($ndistinct) from generate_series(1, $nrows) s(i)"
			fi


      psql $dbname -c "vacuum (analyze,freeze) $tablename";

      SEED=42
      RANDOM=$SEED

			for run in $(seq 1 10); do

				for narray_values in 1 10 100 1000; do

					for step in 1 5 10; do

						if [[ $narray_values -eq 1 && $step -gt 1 ]]; then
							continue
						fi

						range=$((narray_values * step))

						if [[ $range -gt $ndistinct ]]; then
							continue
						fi

						if [[ $ndistinct -gt $range ]]; then
							value=$((RANDOM % (ndistinct - range)))
						else
							value=0
						fi

						values="$value"

						for v in $(seq 1 $((narray_values-1))); do
							value=$((value + step))
							values="$values, $value"
						done

						query="select * from $tablename where v = any('{$values}')"

						echo "----- narray_values $narray_values step $step run $run query $query -----"

						#for scan in indexscan indexonlyscan seqscan bitmapscan; do
						for scan in indexonlyscan ; do
							t=$(query_duration $scan "$query")
							echo "$branch $nrows $ndistinct $type $data $narray_values $step $run $scan $t" >> "$resultfile"
						done

					done

				done

			done

		done

	done

done
