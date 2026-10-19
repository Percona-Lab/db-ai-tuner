#!/usr/bin/env python3
"""Print LOAD DATA statements for the JOB CSV files: gen_load.py <schema.sql> <csv dir>
The CSVs are PostgreSQL COPY CSV with backslash escapes; an empty unquoted field means NULL.
MySQL LOAD DATA reads empty fields as '' (or 0 for INT), so nullable columns go through
@variables and NULLIF(@v, '')."""
import re, sys
schema, d = sys.argv[1], sys.argv[2]
for m in re.finditer(r'CREATE TABLE (\w+) \((.*?)\) ENGINE', open(schema).read(), re.S):
    t, body = m.group(1), m.group(2)
    cols, sets = [], []
    for line in body.strip().splitlines():
        c = line.split()[0]
        if 'NOT NULL' in line:
            cols.append(c)
        else:
            cols.append('@' + c); sets.append(f"{c} = NULLIF(@{c}, '')")
    print(f"LOAD DATA LOCAL INFILE '{d}/{t}.csv' INTO TABLE {t} CHARACTER SET utf8mb4 "
          f"FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' ESCAPED BY '\\\\' LINES TERMINATED BY '\\n' "
          f"({', '.join(cols)})" + (f" SET {', '.join(sets)}" if sets else "") + ";")
