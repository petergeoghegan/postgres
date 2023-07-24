drop table if exists skipscan_costs;
create table skipscan_costs (ndistinct integer, startup_cost numeric, total_cost numeric);

/*
 * small_mdam_idx is 2,382 blocks in size (1/3 of the blocks is 794 blocks)
 */
do $$
  << first_block >>
declare
  qry text;
  ff record;
  film_count integer := 0;
  res text;
  costs numeric[];
begin

  set enable_seqscan to off;
  set enable_bitmapscan to off;

  for i in 1..3000 loop
    qry := 'alter table small_sales_mdam_paper alter column dept set (n_distinct = ' || i || ')';
    execute qry;
    analyze small_sales_mdam_paper;
    execute $f$explain analyze select * from small_sales_mdam_paper where sdate = '1995-01-05' and item_class = 5 and store = 5$f$ into res;
    raise notice '% %', i, res;
    SELECT regexp_match(res, 'cost=([0-9.]+)\.\.([0-9.]+)') into costs;
    raise notice '% % %', i, costs[1], costs[2];
    insert into skipscan_costs(ndistinct, startup_cost, total_cost) select i, costs[1], costs[2];

  end loop;
end first_block
$$;

copy skipscan_costs to '/mnt/nvme/postgresql/patch/source/microbenchmarks/skipscan_costs.csv' delimiter ',' csv header;

/*
  I'll analyze the growth pattern in total_cost as ndistinct increases based on the provided CSV data.

  Looking at the data, I notice several interesting patterns:

  1. From ndistinct=5 to around ndistinct=800, the total_cost increases linearly  <-- we reach "ndistinct = 1/3 of the total pages" threshold
  2. From around ndistinct=800 to ndistinct=2380, the total_cost plateaus at approximately 1727-1729  <-- before "ndistinct = npages" threshold
  3. At ndistinct=2385, there's a dramatic jump in total_cost to approximately 10,407-10,411, which then remains stable until the end of the dataset <-- traditional full index scan

  Let me create a visualization of this relationship:

  ```tsx
  import React from 'react';
  import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

  const CostGrowthChart = () => {
    // Data preparation - using subset of points to improve rendering performance
    const fullData = [
      {ndistinct: 5, total_cost: 37.05},
      {ndistinct: 100, total_cost: 264.36},
      {ndistinct: 200, total_cost: 495.87},
      {ndistinct: 300, total_cost: 719.36},
      {ndistinct: 400, total_cost: 938.86},
      {ndistinct: 500, total_cost: 1148.38},
      {ndistinct: 600, total_cost: 1353.87},
      {ndistinct: 700, total_cost: 1549.36},
      {ndistinct: 750, total_cost: 1649.12},
      {ndistinct: 795, total_cost: 1727.39},
      {ndistinct: 800, total_cost: 1725.37},
      {ndistinct: 900, total_cost: 1729.38},
      {ndistinct: 1000, total_cost: 1727.37},
      {ndistinct: 1500, total_cost: 1729.40},
      {ndistinct: 2000, total_cost: 1725.39},
      {ndistinct: 2380, total_cost: 1729.40},
      {ndistinct: 2385, total_cost: 10407.44},
      {ndistinct: 2500, total_cost: 10409.42},
      {ndistinct: 3000, total_cost: 10407.42}
    ];

    return (
      <div className="flex flex-col space-y-8">
        <div className="h-96 w-full">
          <h2 className="text-xl font-bold mb-4">Skip Scan Total Cost vs. ndistinct</h2>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={fullData}
              margin={{ top: 5, right: 30, left: 20, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="ndistinct"
                label={{ value: 'ndistinct', position: 'insideBottomRight', offset: -10 }}
              />
              <YAxis
                label={{ value: 'total_cost', angle: -90, position: 'insideLeft' }}
              />
              <Tooltip formatter={(value) => value.toFixed(2)} />
              <Legend />
              <Line
                type="monotone"
                dataKey="total_cost"
                stroke="#8884d8"
                activeDot={{ r: 8 }}
                strokeWidth={2}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>

        <div className="h-96 w-full">
          <h2 className="text-xl font-bold mb-4">Initial Linear Growth Phase (ndistinct: 5-800)</h2>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={fullData.filter(d => d.ndistinct <= 800)}
              margin={{ top: 5, right: 30, left: 20, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="ndistinct"
                label={{ value: 'ndistinct', position: 'insideBottomRight', offset: -10 }}
              />
              <YAxis
                label={{ value: 'total_cost', angle: -90, position: 'insideLeft' }}
              />
              <Tooltip formatter={(value) => value.toFixed(2)} />
              <Legend />
              <Line
                type="monotone"
                dataKey="total_cost"
                stroke="#82ca9d"
                activeDot={{ r: 8 }}
                strokeWidth={2}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    );
  };

  export default CostGrowthChart;

  ```

  Based on the visualization and data analysis, the total_cost growth pattern can be characterized as having three distinct phases:

  1. **Linear Growth Phase (ndistinct: 5-800)**:
     - The cost increases approximately linearly with the number of distinct values
     - The growth rate appears to be around 2.2 cost units per ndistinct increment

  2. **Plateau Phase (ndistinct: 800-2380)**:
     - The total_cost stabilizes at approximately 1727-1729 units
     - This suggests the operation reaches some kind of performance ceiling or optimization threshold

  3. **Step Function Jump (at ndistinct ≈ 2385)**:
     - There's a sudden, dramatic increase in cost by about 6x (from ~1729 to ~10409)
     - After this jump, the cost again remains stable regardless of further increases in ndistinct

  This pattern suggests there might be different algorithms or execution strategies being used at different scales of distinct values. The sudden jump at ndistinct=2385 likely represents a threshold where the database engine switches to a different, more expensive execution strategy to handle the larger number of distinct values.

  Would you like me to analyze any specific aspect of this relationship in more detail?
*/
