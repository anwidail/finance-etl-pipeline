"""post the sales_detail journal automatically from the database

``sales_detail`` is maintained by hand in SQL, so the ledger has to follow the
table without anyone remembering to run a command. A stored procedure rebuilds
one invoice's entry, and three triggers call it whenever the table changes:

===============  ==================================================
INSERT / UPDATE  re-post that invoice (``NEW.ref_no``)
DELETE           re-post it too — with no lines left, that removes it
===============  ==================================================

The procedure always **replaces** an invoice's rows rather than adding to them,
so it is safe to fire repeatedly: a bulk insert of N lines re-posts the same
invoice N times and still lands on one correct entry.

It refuses to post an unmapped revenue account, raising SQLSTATE 45000. That
aborts the offending INSERT, which is the point — a line whose account is not in
``coa`` would otherwise post an unbalanced entry.

This mirrors ``transforms.sales_detail`` exactly; the two were compared row for
row over 6,147 ledger rows before this was written. ``load.sales_detail_loader``
detects these triggers and stops posting from Python so the entry is not doubled.

Revision ID: b73f2d95c1ae
Revises: a91c4e73b508
Create Date: 2026-07-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b73f2d95c1ae'
down_revision: Union[str, None] = 'a91c4e73b508'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GL_COLUMNS = """module,type,ref_key,ref_no,date,contact,description,note,dept_code,department,
      project,debit,credit,amount,account_code,coa_name,source_id,source_line_id,
      currency,original_currency,exchange_rate,status,created_at,created_by,coa_code,reporting"""

# gl stores a shortened account code: dashes dropped, trailing zeros trimmed.
_SHORT = "TRIM(TRAILING '0' FROM REPLACE({col}, '-', ''))"

_PROCEDURE = f"""
CREATE PROCEDURE sp_sales_detail_post(IN p_ref VARCHAR(100))
BEGIN
  DECLARE v_date DATE; DECLARE v_contact VARCHAR(200); DECLARE v_dept VARCHAR(200);
  DECLARE v_desc TEXT; DECLARE v_service VARCHAR(260); DECLARE v_status VARCHAR(50);
  DECLARE v_dept_code VARCHAR(100);
  DECLARE v_created DATETIME; DECLARE v_curr VARCHAR(10); DECLARE v_rate DECIMAL(18,6);
  DECLARE v_sub DECIMAL(18,2); DECLARE v_tax DECIMAL(18,2); DECLARE v_missing INT;
  DECLARE v_ar VARCHAR(50); DECLARE v_code VARCHAR(50);
  DECLARE v_name VARCHAR(200); DECLARE v_rep VARCHAR(50);

  DELETE FROM gl WHERE type='sales_detail' AND source_id = p_ref;

  IF EXISTS (SELECT 1 FROM sales_detail WHERE ref_no = p_ref) THEN
    SELECT COUNT(*) INTO v_missing FROM sales_detail s
      LEFT JOIN coa c ON c.account_name = s.account
     WHERE s.ref_no = p_ref AND c.account_code IS NULL;
    IF v_missing > 0 THEN
      SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'sales_detail: revenue account not found in coa';
    END IF;

    -- Invoice-level rows inherit their narrative from the largest line.
    SELECT date, contact, dept, dept_code, description, service, status, created_at,
           COALESCE(currency,'IDR'), COALESCE(exchange_rate,1)
      INTO v_date, v_contact, v_dept, v_dept_code, v_desc, v_service, v_status,
           v_created, v_curr, v_rate
      FROM sales_detail WHERE ref_no = p_ref
     ORDER BY subtotal DESC, service_code DESC LIMIT 1;

    SELECT SUM(subtotal), SUM(tax) INTO v_sub, v_tax
      FROM sales_detail WHERE ref_no = p_ref;

    -- The only account the currency changes.
    SET v_ar = IF(UPPER(v_curr)='IDR', '1121-11-000', '1121-12-000');
    SELECT {_SHORT.format(col='account_code')}, account_name, reporting
      INTO v_code, v_name, v_rep FROM coa WHERE account_code = v_ar LIMIT 1;

    INSERT INTO gl ({_GL_COLUMNS})
    VALUES ('sales_detail','sales_detail',p_ref,p_ref,v_date,v_contact,v_desc,v_service,v_dept_code,v_dept,
      NULL,v_sub+v_tax,0,v_sub+v_tax,v_code,v_name,p_ref,CONCAT(p_ref,'#AR'),
      v_curr,(v_sub+v_tax)/v_rate,v_rate,v_status,v_created,NULL,v_ar,v_rep);

    INSERT INTO gl ({_GL_COLUMNS})
    SELECT 'sales_detail','sales_detail',p_ref,p_ref,v_date,v_contact,s.description,s.service,
      s.dept_code,s.dept,NULL,0,s.subtotal,-s.subtotal,
      {_SHORT.format(col='c.account_code')},c.account_name,
      p_ref,CONCAT(p_ref,'#',s.service_code),v_curr,-s.subtotal/v_rate,v_rate,
      v_status,v_created,NULL,c.account_code,c.reporting
    FROM sales_detail s JOIN coa c ON c.account_name = s.account
    WHERE s.ref_no = p_ref;

    IF v_tax <> 0 THEN
      SELECT {_SHORT.format(col='account_code')}, account_name, reporting
        INTO v_code, v_name, v_rep FROM coa WHERE account_code='2124-11-000' LIMIT 1;
      INSERT INTO gl ({_GL_COLUMNS})
      VALUES ('sales_detail','sales_detail',p_ref,p_ref,v_date,v_contact,v_desc,v_service,v_dept_code,v_dept,
        NULL,0,v_tax,-v_tax,v_code,v_name,p_ref,CONCAT(p_ref,'#VAT'),
        v_curr,-v_tax/v_rate,v_rate,v_status,v_created,NULL,'2124-11-000',v_rep);
    END IF;
  END IF;
END
"""

_TRIGGERS = {
    "trg_sales_detail_post_ins": "AFTER INSERT ON sales_detail FOR EACH ROW "
                                 "CALL sp_sales_detail_post(NEW.ref_no)",
    "trg_sales_detail_post_upd": "AFTER UPDATE ON sales_detail FOR EACH ROW "
                                 "CALL sp_sales_detail_post(NEW.ref_no)",
    "trg_sales_detail_post_del": "AFTER DELETE ON sales_detail FOR EACH ROW "
                                 "CALL sp_sales_detail_post(OLD.ref_no)",
}


def upgrade() -> None:
    op.execute("DROP PROCEDURE IF EXISTS sp_sales_detail_post")
    op.execute(_PROCEDURE)
    for name, body in _TRIGGERS.items():
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP PROCEDURE IF EXISTS sp_sales_detail_post")
