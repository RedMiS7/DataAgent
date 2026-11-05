-- Business database schema for the DataAgent demo shop.
-- All read traffic from the agent goes through the `safe` schema (views only).

CREATE DATABASE IF NOT EXISTS shop DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS safe DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE shop;

CREATE TABLE IF NOT EXISTS categories (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    name        VARCHAR(64) NOT NULL,
    parent_id   INT NULL,
    created_at  DATETIME NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS channels (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    name        VARCHAR(64) NOT NULL,
    type        VARCHAR(16) NOT NULL,
    created_at  DATETIME NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS products (
    id           INT PRIMARY KEY AUTO_INCREMENT,
    sku          VARCHAR(32) NOT NULL,
    name         VARCHAR(128) NOT NULL,
    category_id  INT NOT NULL,
    price        DECIMAL(10, 2) NOT NULL,
    cost         DECIMAL(10, 2) NOT NULL,
    launch_date  DATE NOT NULL,
    status       TINYINT NOT NULL DEFAULT 1,
    UNIQUE KEY uk_products_sku (sku),
    KEY idx_products_category (category_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS customers (
    id             INT PRIMARY KEY AUTO_INCREMENT,
    name           VARCHAR(64) NOT NULL,
    phone          VARCHAR(20) NOT NULL,
    province       VARCHAR(32) NOT NULL,
    city           VARCHAR(32) NOT NULL,
    address        VARCHAR(255) NOT NULL,
    registered_at  DATETIME NOT NULL,
    KEY idx_customers_province (province)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS orders (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_no      VARCHAR(32) NOT NULL,
    customer_id   INT NOT NULL,
    channel_id    INT NOT NULL,
    status        VARCHAR(16) NOT NULL,
    ordered_at    DATETIME NOT NULL,
    paid_at       DATETIME NULL,
    total_amount  DECIMAL(12, 2) NOT NULL,
    shipping_fee  DECIMAL(8, 2) NOT NULL DEFAULT 0,
    province      VARCHAR(32) NOT NULL,
    city          VARCHAR(32) NOT NULL,
    UNIQUE KEY uk_orders_no (order_no),
    KEY idx_orders_paid_at (paid_at),
    KEY idx_orders_channel (channel_id),
    KEY idx_orders_province (province),
    KEY idx_orders_status (status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS order_items (
    id               BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id         BIGINT NOT NULL,
    product_id       INT NOT NULL,
    quantity         INT NOT NULL,
    unit_price       DECIMAL(10, 2) NOT NULL,
    discount_amount  DECIMAL(10, 2) NOT NULL DEFAULT 0,
    KEY idx_items_order (order_id),
    KEY idx_items_product (product_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS refunds (
    id             BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id       BIGINT NOT NULL,
    order_item_id  BIGINT NOT NULL,
    product_id     INT NOT NULL,
    reason         VARCHAR(64) NOT NULL,
    refund_amount  DECIMAL(10, 2) NOT NULL,
    requested_at   DATETIME NOT NULL,
    completed_at   DATETIME NULL,
    status         VARCHAR(16) NOT NULL,
    KEY idx_refunds_completed (completed_at),
    KEY idx_refunds_product (product_id),
    KEY idx_refunds_order (order_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS shipments (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    order_id      BIGINT NOT NULL,
    carrier       VARCHAR(32) NOT NULL,
    tracking_no   VARCHAR(64) NOT NULL,
    shipped_at    DATETIME NOT NULL,
    delivered_at  DATETIME NULL,
    province      VARCHAR(32) NOT NULL,
    KEY idx_shipments_order (order_id),
    KEY idx_shipments_province (province)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ad_campaigns (
    id            INT PRIMARY KEY AUTO_INCREMENT,
    campaign_name VARCHAR(128) NOT NULL,
    channel_id    INT NOT NULL,
    product_id    INT NOT NULL,
    daily_budget  DECIMAL(10, 2) NOT NULL,
    start_date    DATE NOT NULL,
    end_date      DATE NOT NULL,
    KEY idx_campaigns_channel (channel_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ad_daily_stats (
    id                 BIGINT PRIMARY KEY AUTO_INCREMENT,
    campaign_id        INT NOT NULL,
    stat_date          DATE NOT NULL,
    impressions        INT NOT NULL,
    clicks             INT NOT NULL,
    spend              DECIMAL(12, 2) NOT NULL,
    attributed_gmv     DECIMAL(12, 2) NOT NULL,
    attributed_orders  INT NOT NULL,
    UNIQUE KEY uk_ad_stats (campaign_id, stat_date),
    KEY idx_ad_stats_date (stat_date)
) ENGINE=InnoDB;

-- Masked views: the read-only account never sees the base tables.
CREATE OR REPLACE VIEW safe.categories AS
SELECT id, name, parent_id, created_at FROM shop.categories;

CREATE OR REPLACE VIEW safe.channels AS
SELECT id, name, type, created_at FROM shop.channels;

CREATE OR REPLACE VIEW safe.products AS
SELECT id, sku, name, category_id, price, cost, launch_date, status FROM shop.products;

CREATE OR REPLACE VIEW safe.customers AS
SELECT id, name, province, city, registered_at FROM shop.customers;

CREATE OR REPLACE VIEW safe.orders AS
SELECT id, order_no, customer_id, channel_id, status, ordered_at, paid_at,
       total_amount, shipping_fee, province, city FROM shop.orders;

CREATE OR REPLACE VIEW safe.order_items AS
SELECT id, order_id, product_id, quantity, unit_price, discount_amount FROM shop.order_items;

CREATE OR REPLACE VIEW safe.refunds AS
SELECT id, order_id, order_item_id, product_id, reason, refund_amount,
       requested_at, completed_at, status FROM shop.refunds;

CREATE OR REPLACE VIEW safe.shipments AS
SELECT id, order_id, carrier, tracking_no, shipped_at, delivered_at, province FROM shop.shipments;

CREATE OR REPLACE VIEW safe.ad_campaigns AS
SELECT id, campaign_name, channel_id, product_id, daily_budget, start_date, end_date FROM shop.ad_campaigns;

CREATE OR REPLACE VIEW safe.ad_daily_stats AS
SELECT id, campaign_id, stat_date, impressions, clicks, spend, attributed_gmv,
       attributed_orders FROM shop.ad_daily_stats;