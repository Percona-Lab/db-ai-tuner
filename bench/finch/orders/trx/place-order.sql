BEGIN

-- prepare
-- save-columns: @price
SELECT price_cents FROM products WHERE id = @prod

-- prepare
INSERT INTO orders (customer_id, product_id, qty, amount_cents, note, created_at) VALUES (@cust, @prod, @qty, @price * @qty, @note, NOW())

-- prepare
UPDATE products SET stock = stock - @qty WHERE id = @prod

-- prepare
UPDATE customers SET balance_cents = balance_cents - @price * @qty WHERE id = @cust

-- prepare
UPDATE counters SET val = val + 1 WHERE id = @hot

COMMIT
