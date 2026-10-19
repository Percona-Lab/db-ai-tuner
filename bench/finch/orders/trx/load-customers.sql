-- rows: ${params.customers}
INSERT INTO orders.customers (name, email, city_id) VALUES /*!csv 500 (@name, @email, @city)*/
