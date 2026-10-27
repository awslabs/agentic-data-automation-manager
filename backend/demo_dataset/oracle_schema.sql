-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: Apache-2.0
--
-- Core banking schema for the AnyCompany demo (Oracle, schema MCP_DEV).
-- Run as MCP_DEV. Assumes the DBA has already created the RELATIONSHIP_MANAGER and
-- SERVICING_ANALYST database users; this script only grants to them.
--
-- For a fresh environment only. build.py does not run this: it reloads the rows of
-- an existing MCP_DEV and leaves its DDL alone.
--
-- Conventions the generator enforces (no constraint can, across rows):
--   TRANSACTIONS.AMOUNT is signed; RUNNING_BALANCE = previous + AMOUNT, and the
--     latest RUNNING_BALANCE of an account equals ACCOUNTS.BALANCE.
--   LINE_OF_CREDIT balances are negative: the amount owed.
--   CLIENT_CONTACTS.RM_ID is the client's own RELATIONSHIP_MANAGER_ID.
--   CLIENT_ID is the key shared with the wealth platform (Aurora); no other key is.

CREATE TABLE relationship_managers (
    rm_id    NUMBER(10)    PRIMARY KEY,
    name     VARCHAR2(150) NOT NULL,
    email    VARCHAR2(200),
    branch   VARCHAR2(100),
    region   VARCHAR2(50),
    team     VARCHAR2(50)
);

CREATE TABLE clients (
    client_id               NUMBER(10)    PRIMARY KEY,
    first_name              VARCHAR2(100) NOT NULL,
    last_name               VARCHAR2(100) NOT NULL,
    email                   VARCHAR2(200),
    phone                   VARCHAR2(30),
    date_of_birth           DATE,
    risk_profile            VARCHAR2(20)
        CHECK (risk_profile IN ('CONSERVATIVE','MODERATE','GROWTH','AGGRESSIVE')),
    relationship_manager_id NUMBER(10) REFERENCES relationship_managers (rm_id),
    segment                 VARCHAR2(30)
        CHECK (segment IN ('RETAIL','PREFERRED','PRIVATE_BANKING','INSTITUTIONAL')),
    onboarded_date          DATE,
    status                  VARCHAR2(20) DEFAULT 'ACTIVE'
);

CREATE TABLE accounts (
    account_id     NUMBER(10)   PRIMARY KEY,
    client_id      NUMBER(10)   NOT NULL REFERENCES clients (client_id),
    account_type   VARCHAR2(30)
        CHECK (account_type IN ('CHECKING','SAVINGS','TERM_DEPOSIT','MONEY_MARKET','LINE_OF_CREDIT')),
    account_number VARCHAR2(20) NOT NULL UNIQUE,
    currency       VARCHAR2(3)  DEFAULT 'USD',
    balance        NUMBER(14,2),
    interest_rate  NUMBER(5,3),
    opened_date    DATE,
    maturity_date  DATE,
    status         VARCHAR2(20) DEFAULT 'ACTIVE'
);

CREATE TABLE transactions (
    txn_id          NUMBER(10)    PRIMARY KEY,
    account_id      NUMBER(10)    NOT NULL REFERENCES accounts (account_id),
    txn_date        DATE          NOT NULL,
    txn_type        VARCHAR2(30)
        CHECK (txn_type IN ('DEPOSIT','WITHDRAWAL','TRANSFER','PAYMENT','INTEREST','FEE')),
    amount          NUMBER(14,2)  NOT NULL,
    running_balance NUMBER(14,2),
    description     VARCHAR2(500),
    reference       VARCHAR2(50)
);

CREATE TABLE client_contacts (
    contact_id   NUMBER(10)     PRIMARY KEY,
    client_id    NUMBER(10)     NOT NULL REFERENCES clients (client_id),
    contact_date DATE,
    contact_type VARCHAR2(30)
        CHECK (contact_type IN ('MEETING','CALL','EMAIL','REVIEW')),
    notes        VARCHAR2(4000),
    rm_id        NUMBER(10) REFERENCES relationship_managers (rm_id)
);

-- Milestones in the client's profile, as a CRM records them. Recorded by the
-- client's own RM. Planned events share their date with the goal they relate to
-- (PLANNED_RETIREMENT = the RETIREMENT goal's target_date); CHILD_TO_COLLEGE is
-- the 18th birthday of a child recorded by a NEW_CHILD event.
CREATE TABLE client_life_events (
    event_id    NUMBER(10)   PRIMARY KEY,
    client_id   NUMBER(10)   NOT NULL REFERENCES clients (client_id),
    event_type  VARCHAR2(30) NOT NULL
        CHECK (event_type IN ('MARRIAGE','NEW_CHILD','CHILD_TO_COLLEGE','HOME_PURCHASE',
                              'PLANNED_RETIREMENT')),
    event_date  DATE         NOT NULL,
    notes       VARCHAR2(1000),
    rm_id       NUMBER(10)   REFERENCES relationship_managers (rm_id)
);

-- Personal information: the relationship manager only.
GRANT SELECT ON client_life_events TO relationship_manager;

-- The relationship manager sees the whole client. The servicing analyst services
-- every account (accounts, balances, transactions, contact history) but sees neither
-- the client's personal details nor their life events.
GRANT SELECT ON clients               TO relationship_manager;
GRANT SELECT ON accounts              TO relationship_manager;
GRANT SELECT ON relationship_managers TO relationship_manager;
GRANT SELECT ON transactions          TO relationship_manager;
GRANT SELECT ON client_contacts       TO relationship_manager;

GRANT SELECT ON accounts              TO servicing_analyst;
GRANT SELECT ON transactions          TO servicing_analyst;
GRANT SELECT ON client_contacts       TO servicing_analyst;

-- Column-level restriction for the servicing analyst: who the client is, without
-- email, phone or date of birth.
CREATE OR REPLACE VIEW v_clients_restricted AS
SELECT client_id, first_name, last_name, segment, relationship_manager_id, status
FROM clients;

GRANT SELECT ON v_clients_restricted  TO servicing_analyst;
