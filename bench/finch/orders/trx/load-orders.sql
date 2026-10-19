-- rows: ${params.orders}
INSERT INTO orders.orders (customer_id, product_id, qty, amount_cents, status, note, created_at) VALUES /*!csv 500 (@cust, @prod, @qty, @amt, 'done', @note, NOW() - INTERVAL @days DAY)*/
