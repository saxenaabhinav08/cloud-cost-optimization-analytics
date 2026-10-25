-- All queries return fictional sample results when run against the included CSV.

-- Monthly spending across all services.
SELECT billing_month, SUM(cost_usd) AS total_cost_usd
FROM billing_records
GROUP BY billing_month
ORDER BY billing_month;

-- Spending by AWS service and month.
SELECT billing_month, service, SUM(cost_usd) AS total_cost_usd
FROM billing_records
GROUP BY billing_month, service
ORDER BY billing_month, total_cost_usd DESC;

-- Spending by region and month.
SELECT billing_month, region, SUM(cost_usd) AS total_cost_usd
FROM billing_records
GROUP BY billing_month, region
ORDER BY billing_month, total_cost_usd DESC;

-- Month-over-month change in total spend.
WITH monthly_totals AS (
    SELECT billing_month, SUM(cost_usd) AS total_cost_usd
    FROM billing_records
    GROUP BY billing_month
),
with_previous AS (
    SELECT billing_month,
           total_cost_usd,
           LAG(total_cost_usd) OVER (ORDER BY billing_month) AS previous_month_cost_usd
    FROM monthly_totals
)
SELECT billing_month,
       total_cost_usd,
       previous_month_cost_usd,
       total_cost_usd - previous_month_cost_usd AS change_usd,
       CASE
           WHEN previous_month_cost_usd IS NULL OR previous_month_cost_usd = 0 THEN NULL
           ELSE 100.0 * (total_cost_usd - previous_month_cost_usd) / previous_month_cost_usd
       END AS change_percent
FROM with_previous
ORDER BY billing_month;
