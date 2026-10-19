SELECT id, name, email, city_id, balance_cents FROM customers WHERE id = @cust

SELECT id, product_id, qty, amount_cents, status, created_at FROM orders WHERE customer_id = @cust ORDER BY created_at DESC LIMIT 10

SELECT id, name, price_cents, stock FROM products WHERE id = @prod

BEGIN

INSERT INTO orders (customer_id, product_id, qty, amount_cents, note, created_at) VALUES (@cust, @prod, @qty, 1000 * @qty, @note, NOW())

UPDATE counters SET val = val + 1 WHERE id = @hot

COMMIT
