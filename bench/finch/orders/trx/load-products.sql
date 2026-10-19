-- rows: ${params.products}
INSERT INTO orders.products (name, price_cents) VALUES /*!csv 500 (@pname, @price)*/
