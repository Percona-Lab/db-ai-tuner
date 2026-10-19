#!/usr/bin/env python3
"""Add connection churn to Finch: FINCH_RECONNECT_EVERY=N makes every client close its MySQL
connection (physically, not back to the pool) and open a new one every N iterations.
Upstream Finch reconnects only after errors (with a 200 ms pause), so it cannot model
connect-per-request applications. Usage: apply_reconnect_patch.py <finch source dir>"""
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / "client" / "client.go"
s = p.read_text()
if "FINCH_RECONNECT_EVERY" in s:
    sys.exit(0)
s = s.replace('import (\n\t"context"\n\t"database/sql"\n',
              'import (\n\t"context"\n\t"database/sql"\n\t"database/sql/driver"\n\t"os"\n\t"strconv"\n', 1)
s = s.replace('''var (
	ConnectTimeout   = 500 * time.Millisecond''', '''var ReconnectEvery = func() uint {
	n, _ := strconv.Atoi(os.Getenv("FINCH_RECONNECT_EVERY"))
	if n < 0 {
		n = 0
	}
	return uint(n)
}()

// churn closes the connection for real (ErrBadConn makes database/sql discard it instead of
// pooling it), drops its prepared statements and connects again.
func (c *Client) churn(ctx context.Context) error {
	for j := range c.ps {
		if c.ps[j] != nil {
			c.ps[j].Close()
			c.ps[j] = nil
		}
	}
	if c.conn != nil {
		c.conn.Raw(func(any) error { return driver.ErrBadConn })
		c.conn.Close()
		c.conn = nil
	}
	return c.Connect(ctx, nil, -1, false)
}

var (
	ConnectTimeout   = 500 * time.Millisecond''', 1)
old = '''		rc[data.ITER] += 1
		trxNo = -1
'''
assert old in s
s = s.replace(old, '''		rc[data.ITER] += 1
		if ReconnectEvery > 0 && rc[data.ITER] > 1 && (rc[data.ITER]-1)%ReconnectEvery == 0 {
			if err = c.churn(ctxExec); err != nil {
				return
			}
			rc[data.CONN] += 1
		}
		trxNo = -1
''', 1)
p.write_text(s)
