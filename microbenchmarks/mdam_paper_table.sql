-- See also: refined regression test with MDAM test table + various queries:
-- src/test/regress/sql/mdam_paper_table.sql

drop table if exists sales_mdam_paper;
create unlogged table sales_mdam_paper
(
  dept int4,
  sdate date,
  item_class serial,
  store int4,
  item int4,
  total_sales numeric
);
create index mdam_idx on sales_mdam_paper(dept, sdate, item_class, store);

-- Duration of INSERT with 900,000,000 rows:
--
-- INSERT 0 900000000
-- Time: 2524219.804 ms (42:04.220)

insert into sales_mdam_paper (dept, sdate, item_class, store, total_sales)
-- total_sales is pretty much just a filler column:
-- omit "item" (which is serial column):
select
  dept,
  '1995-01-01'::date + sdate,
  item_class,
  store,
  (random() * 500.0) as total_sales
from
  -- "So let us assume that the values for ,the column dept in the table range from 1 through 100":
  generate_series(1, 100) dept,
  -- 400 days, starting on Jan 1 of 95:
  generate_series(1, 400) sdate,
  -- Highest item_class in paper is 50, so arbitrarily assume 75 total:
  generate_series(1, 75) item_class,
  -- Highest store in paper is 250, so arbitrarily assume 300 total:
  generate_series(1, 300) store;

/*
:ea select
  sdate,
  item_class,
  store,
  sum(total_sales)
from
  sales_mdam_paper
where

  sdate between '1995-06-01' and '1995-06-30'
  and item_class in (20, 35, 50)
  and store in (200, 250)
group by
  sdate,
  item_class,
  store;
*/
