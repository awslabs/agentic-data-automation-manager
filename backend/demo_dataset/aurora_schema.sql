-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: Apache-2.0
--
-- Wealth platform schema for the AnyCompany demo (Aurora PostgreSQL, schema wealth).
-- Run as the schema owner (mcp_dev). Assumes the relationship_manager and
-- servicing_analyst roles already exist; this script only grants to them.
--
-- DESTRUCTIVE: drops and recreates every wealth-platform object. build.py runs it
-- before loading data.
--
-- Principle: store facts once, derive totals in views.
--   * A goal's progress is the market value of the accounts that fund it
--     (investment_accounts.goal_id), not a number typed into the goal.
--   * A security has one price (securities); positions hold quantity and cost only.
--   * trades are the ledger: positions.quantity = net BUY - SELL per account and
--     symbol, and cost_basis is the average cost of the buys. Enforced by build.py.
--   * client_id and rm_id refer to Oracle MCP_DEV.CLIENTS / RELATIONSHIP_MANAGERS.
--     No foreign key can cross engines; build.py guarantees both.

CREATE SCHEMA IF NOT EXISTS wealth;
SET search_path TO wealth;

DROP VIEW IF EXISTS v_goal_progress;
DROP VIEW IF EXISTS v_positions_summary;
DROP VIEW IF EXISTS v_investment_accounts_restricted;
DROP VIEW IF EXISTS v_positions;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS advisory_activity;
DROP TABLE IF EXISTS investment_accounts;
DROP TABLE IF EXISTS portfolio_goals;
DROP TABLE IF EXISTS securities;

CREATE TABLE securities (
    symbol        varchar(10)   PRIMARY KEY,
    security_name varchar(200)  NOT NULL,
    asset_class   varchar(30)   NOT NULL
        CHECK (asset_class IN ('EQUITY','FIXED_INCOME','MUTUAL_FUND','ETF','ALTERNATIVE','CASH')),
    current_price numeric(14,4) NOT NULL CHECK (current_price > 0),
    price_date    date          NOT NULL
);

CREATE TABLE portfolio_goals (
    goal_id       serial        PRIMARY KEY,
    client_id     integer       NOT NULL,
    goal_type     varchar(30)   NOT NULL
        CHECK (goal_type IN ('RETIREMENT','EDUCATION','HOME_PURCHASE','WEALTH_PRESERVATION','INCOME')),
    target_amount numeric(14,2) NOT NULL CHECK (target_amount > 0),
    target_date   date,
    priority      varchar(10)   CHECK (priority IN ('HIGH','MEDIUM','LOW'))
);

CREATE TABLE investment_accounts (
    account_id     serial      PRIMARY KEY,
    client_id      integer     NOT NULL,
    account_type   varchar(30) NOT NULL
        CHECK (account_type IN ('BROKERAGE','IRA','ROTH_IRA','401K','TRUST')),
    account_number varchar(20) NOT NULL UNIQUE,
    opened_date    date,
    status         varchar(20) DEFAULT 'ACTIVE',
    -- The goal this account's assets are earmarked for. NULL for general savings.
    goal_id        integer     REFERENCES portfolio_goals (goal_id)
);

CREATE TABLE positions (
    position_id serial        PRIMARY KEY,
    account_id  integer       NOT NULL REFERENCES investment_accounts (account_id),
    symbol      varchar(10)   NOT NULL REFERENCES securities (symbol),
    quantity    numeric(14,4) NOT NULL CHECK (quantity > 0),
    cost_basis  numeric(14,2) NOT NULL,
    UNIQUE (account_id, symbol)
);

CREATE TABLE trades (
    trade_id        serial        PRIMARY KEY,
    account_id      integer       NOT NULL REFERENCES investment_accounts (account_id),
    symbol          varchar(10)   NOT NULL REFERENCES securities (symbol),
    trade_date      date          NOT NULL,
    settlement_date date,
    trade_type      varchar(10)   NOT NULL
        CHECK (trade_type IN ('BUY','SELL','DIVIDEND','SPLIT')),
    -- Shares for BUY/SELL; 0 for DIVIDEND, whose value is total_amount.
    quantity        numeric(14,4) NOT NULL,
    price           numeric(14,4),
    total_amount    numeric(14,2) NOT NULL,
    status          varchar(20)   DEFAULT 'SETTLED'
);

CREATE TABLE advisory_activity (
    activity_id   serial      PRIMARY KEY,
    client_id     integer     NOT NULL,
    activity_date date        NOT NULL,
    activity_type varchar(30) NOT NULL
        CHECK (activity_type IN ('PORTFOLIO_REVIEW','REBALANCE','RECOMMENDATION','RISK_ASSESSMENT','GOAL_UPDATE')),
    summary       text,
    -- Oracle MCP_DEV.RELATIONSHIP_MANAGERS.RM_ID: always the client's own RM.
    rm_id         integer     NOT NULL
);

-- Positions with market data, in the column set positions used to carry.
CREATE VIEW v_positions AS
SELECT p.position_id,
       p.account_id,
       p.symbol,
       s.security_name,
       s.asset_class,
       p.quantity,
       p.cost_basis,
       s.current_price,
       ROUND(p.quantity * s.current_price, 2)                AS market_value,
       ROUND(p.quantity * s.current_price, 2) - p.cost_basis AS unrealized_pnl,
       s.price_date                                          AS last_updated
FROM positions p
JOIN securities s USING (symbol);

CREATE VIEW v_positions_summary AS
SELECT vp.account_id,
       ia.client_id,
       vp.asset_class,
       COUNT(*)               AS num_positions,
       SUM(vp.market_value)   AS total_market_value,
       SUM(vp.unrealized_pnl) AS total_unrealized_pnl
FROM v_positions vp
JOIN investment_accounts ia USING (account_id)
GROUP BY vp.account_id, ia.client_id, vp.asset_class;

-- investment_accounts without the goal each account is earmarked for, for the
-- servicing analyst: they service the account, but goals are planning.
CREATE VIEW v_investment_accounts_restricted AS
SELECT account_id, client_id, account_type, opened_date, status, account_number
FROM investment_accounts;

-- A goal's progress: what the accounts earmarked for it are worth today.
CREATE VIEW v_goal_progress AS
SELECT g.goal_id,
       g.client_id,
       g.goal_type,
       g.target_amount,
       g.target_date,
       g.priority,
       COALESCE(SUM(vp.market_value), 0)                                   AS current_value,
       ROUND(100 * COALESCE(SUM(vp.market_value), 0) / g.target_amount, 1) AS pct_of_target,
       COALESCE(SUM(vp.market_value), 0) >= g.target_amount                 AS target_met
FROM portfolio_goals g
LEFT JOIN investment_accounts ia ON ia.goal_id = g.goal_id
LEFT JOIN v_positions vp ON vp.account_id = ia.account_id
GROUP BY g.goal_id, g.client_id, g.goal_type, g.target_amount, g.target_date, g.priority;

GRANT USAGE ON SCHEMA wealth TO relationship_manager, servicing_analyst;

-- The relationship manager sees the whole client. The servicing analyst sees every
-- account, holding and trade, but not goals or goal progress.
GRANT SELECT ON securities, portfolio_goals, investment_accounts, positions, trades,
                advisory_activity, v_positions, v_positions_summary, v_goal_progress
      TO relationship_manager;

GRANT SELECT ON securities, positions, trades, advisory_activity, v_positions,
                v_positions_summary, v_investment_accounts_restricted
      TO servicing_analyst;
