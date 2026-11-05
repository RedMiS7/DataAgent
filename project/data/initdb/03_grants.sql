-- Read-only account for the agent. Only the `safe` views are granted.
-- The password here must match MYSQL_RO_PASSWORD in project/.env.

CREATE USER IF NOT EXISTS 'shop_ro'@'%' IDENTIFIED BY 'dataagent_ro_pwd';
ALTER USER 'shop_ro'@'%' IDENTIFIED BY 'dataagent_ro_pwd';
GRANT SELECT ON safe.* TO 'shop_ro'@'%';
FLUSH PRIVILEGES;