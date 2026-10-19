ALTER TABLE orders.orders ADD INDEX customer_created (customer_id, created_at), ADD INDEX created (created_at)

ALTER TABLE orders.customers ADD UNIQUE INDEX email (email)

ANALYZE TABLE orders.customers, orders.products, orders.counters, orders.orders
