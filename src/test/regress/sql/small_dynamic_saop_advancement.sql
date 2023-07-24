--set enable_bitmapscan to off;
--set enable_indexonlyscan to off;
--set enable_indexscan to off;
set enable_seqscan=off;

-- (September 14)
--
-- Here we see contradictory scan keys on the column "two" during the start of
-- the first would-be primitive index scan where two = 1, but not any earlier
-- primitive scans.
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two < 1
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

-- (September 14) Similar to above, but it's earlier value of two, not later
-- ones
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two > 0
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

-- (September 14) Similar to above, but it's an equality this time
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two = 1
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;

-- (September 14) Similar to above, but it's multiple SAOP equalities this time
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and two in (1, 2)
  and four in (1, 2, 3)
group by two, four, twenty, hundred
order by two, four, twenty, hundred;

select count(*), a, b, c
from
  functional_dependencies
where
  a = any (array[1, 26, 51, 76])
  and b = any (array['1', '26'])
  and c = 1
group by a, b, c;

-- (October 18) Similar to above, but tries to break lack of support for the
-- full set of _bt_preprocess_keys() push-ups in stripped down version of
-- function:
select count(*), two, four, twenty, hundred
from
  tenk1_dyn_saop
where
  two in (0, 1) and four in (1, 2, 3)
  and two in(-1,0)
  and twenty in (1, 2, 5, 7, 8, 11, 12, 13, 14, 17)
  and hundred in (1, 3, 4, 9, 14, 51, 90, 88, 41, 39, 22)
group by
  two, four, twenty, hundred
order by
  two, four, twenty, hundred;
