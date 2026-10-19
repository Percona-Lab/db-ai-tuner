-- prepare
SELECT id, name, email, city_id, balance_cents FROM customers WHERE id = @bcust

-- prepare
SELECT id, product_id, qty, amount_cents, status, created_at FROM orders WHERE customer_id = @bcust ORDER BY created_at DESC LIMIT 10

-- prepare
-- copies: 3
SELECT id, name, price_cents, stock FROM products WHERE id = @bprod

-- prepare
SELECT id, customer_id, amount_cents, status FROM orders WHERE id = @oid
