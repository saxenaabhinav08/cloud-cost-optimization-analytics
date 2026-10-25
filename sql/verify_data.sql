SELECT
    DATE_FORMAT(billing_month, '%Y-%m') AS billing_month,
    SUM(cost_usd) AS total_cost_usd
FROM billing_records
GROUP BY billing_month
ORDER BY billing_month;