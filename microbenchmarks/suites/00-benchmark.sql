-- Ensure deterministic data loading across PG versions
SET synchronize_seqscans = off;

-- Drop existing tables
DROP TABLE IF EXISTS prefetch_orders CASCADE;
DROP TABLE IF EXISTS prefetch_customers CASCADE;
DROP TABLE IF EXISTS prefetch_products CASCADE;
DROP TABLE IF EXISTS prefetch_sequential CASCADE;
DROP TABLE IF EXISTS prefetch_sparse CASCADE;

-- Main fact table: ~50M rows with low fillfactor
CREATE TABLE prefetch_orders (
  order_id bigint,
  customer_id int,
  product_id int,
  order_date date,
  region_id int,
  amount numeric(10,2)
) WITH (fillfactor = 40);

-- Dimension tables
CREATE TABLE prefetch_customers (
  customer_id int PRIMARY KEY,
  region_id int,
  customer_name text
);

CREATE TABLE prefetch_products (
  product_id int PRIMARY KEY,
  category_id int,
  product_name text
);

-- Load customers (100K)
INSERT INTO prefetch_customers (customer_id, region_id, customer_name)
SELECT i, (i % 20) + 1, 'Customer_' || i
FROM generate_series(1, 100000) i;

-- Load products (10K)
INSERT INTO prefetch_products (product_id, category_id, product_name)
SELECT i, (i % 50) + 1, 'Product_' || i
FROM generate_series(1, 10000) i;

-- Set deterministic seed
SELECT setseed(0.5);

-- Load orders with controlled scatter pattern
INSERT INTO prefetch_orders (order_id, customer_id, product_id, order_date, region_id, amount)
SELECT
  row_number() over () as order_id,
  customer_id,
  product_id,
  order_date,
  (customer_id % 20) + 1 as region_id,
  (random() * 1000)::numeric(10,2) as amount
FROM (
  SELECT
    ((g.i - 1) % 100000) + 1 as customer_id,
    ((g.i - 1) % 10000) + 1 as product_id,
    '2023-01-01'::date + ((g.i - 1) % 730) as order_date
  FROM generate_series(1, 50000000) g(i)
  ORDER BY (g.i / 32) + (random() * 4 - 2)::int, g.i
) sub;

-- Create indexes
CREATE INDEX prefetch_orders_cust_date_idx
  ON prefetch_orders(customer_id, order_date)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_date_idx
  ON prefetch_orders(order_date)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_prod_idx
  ON prefetch_orders(product_id)
  WITH (deduplicate_items=off);

CREATE INDEX prefetch_orders_id_idx
  ON prefetch_orders(order_id)
  WITH (deduplicate_items=off);

-- VACUUM FREEZE ANALYZE
VACUUM FREEZE ANALYZE prefetch_orders;
VACUUM FREEZE ANALYZE prefetch_customers;
VACUUM FREEZE ANALYZE prefetch_products;

-- Adversarial table: sequential heap access
CREATE TABLE prefetch_sequential (
  id bigint,
  val1 int,
  val2 text
);
INSERT INTO prefetch_sequential
SELECT i, i % 1000, 'value_' || i
FROM generate_series(1, 500000) i;
CREATE INDEX prefetch_sequential_idx ON prefetch_sequential(id);
VACUUM ANALYZE prefetch_sequential;

-- Adversarial table: sparse (1 TID per block)
CREATE TABLE prefetch_sparse (
  id bigint,
  category int,
  padding text
);
ALTER TABLE prefetch_sparse ALTER COLUMN padding SET STORAGE plain;
SELECT setseed(0.7890123456789012);
INSERT INTO prefetch_sparse
SELECT i, (i % 50) + 1, repeat('x', 4000)
FROM generate_series(1, 50000) i
ORDER BY random();
CREATE INDEX prefetch_sparse_cat_idx ON prefetch_sparse(category);
VACUUM ANALYZE prefetch_sparse;

CHECKPOINT;
