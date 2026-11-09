-- Read-only account for the agent. Only the `safe` views are granted.
-- The password here must match MYSQL_RO_PASSWORD in project/.env.

CREATE USER IF NOT EXISTS 'shop_ro'@'%' IDENTIFIED BY 'dataagent_ro_pwd';
ALTER USER 'shop_ro'@'%' IDENTIFIED BY 'dataagent_ro_pwd';
GRANT SELECT ON safe.* TO 'shop_ro'@'%';

-- Account the service runs with. It reads and writes the app database, may add the
-- tables and columns start-up checks for, and fills the empty `shop` database with
-- demo orders. It cannot drop tables or grant rights; root is only for admin commands.
-- The password here must match MYSQL_APP_PASSWORD in project/.env. Databases created
-- before this account existed get it from: python -m app.semantic.cli grant_app_user
-- The rights must match backend/app/db/accounts.py.

CREATE USER IF NOT EXISTS 'dataagent_app'@'%' IDENTIFIED BY 'dataagent_app_pwd';
ALTER USER 'dataagent_app'@'%' IDENTIFIED BY 'dataagent_app_pwd';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON dataagent.* TO 'dataagent_app'@'%';
GRANT SELECT, INSERT ON shop.* TO 'dataagent_app'@'%';
FLUSH PRIVILEGES;
