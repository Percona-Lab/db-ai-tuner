-- sysbench 1.0 connection storm: every event opens a new connection, runs 5 point selects and disconnects
-- (sysbench 1.0 has no --reconnect option)
sysbench.cmdline.options = {
  tables = {"Number of tables", 4},
  table_size = {"Rows per table", 1000000},
}
function thread_init()
  drv = sysbench.sql.driver()
end
function event()
  local con = drv:connect()
  for i = 1, 5 do
    local t = sysbench.rand.uniform(1, sysbench.opt.tables)
    con:query(string.format("SELECT c FROM sbtest%d WHERE id=%d", t, sysbench.rand.default(1, sysbench.opt.table_size)))
  end
  con:disconnect()
end
