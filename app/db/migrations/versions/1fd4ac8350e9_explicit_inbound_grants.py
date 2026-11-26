"""Preserve the effective inbound assignments at the time of upgrade."""
from alembic import op
import sqlalchemy as sa

revision = "1fd4ac8350e9"
down_revision = "2b231de97dc3"
branch_labels = None
depends_on = None


def active_inbounds():
    from app import xray
    return [(tag, inbound["protocol"]) for tag, inbound in xray.config.inbounds_by_tag.items()]


def upgrade():
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("allowed_inbounds_association"):
        op.create_table(
            "allowed_inbounds_association",
            sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("proxies.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("inbound_tag", sa.String(256), sa.ForeignKey("inbounds.tag", ondelete="CASCADE"), primary_key=True),
        )
    for tag, protocol in active_inbounds():
        params = {"tag": tag, "protocol": protocol}
        if not conn.execute(sa.text("SELECT 1 FROM inbounds WHERE tag=:tag"), params).first():
            # Preserve the host a stock panel would generate on first access.
            conn.execute(sa.text("INSERT INTO inbounds(tag) VALUES(:tag)"), params)
            conn.execute(sa.text("""
                INSERT INTO hosts(inbound_tag,remark,address,is_disabled)
                VALUES(:tag,'🚀 Marz ({USERNAME}) [{PROTOCOL} - {TRANSPORT}]','{SERVER_IP}',0)
            """), params)
        conn.execute(sa.text("""
            INSERT INTO allowed_inbounds_association(proxy_id,inbound_tag)
            SELECT p.id,:tag FROM proxies p WHERE lower(p.type)=:protocol
            AND NOT EXISTS (SELECT 1 FROM exclude_inbounds_association e
                            WHERE e.proxy_id=p.id AND e.inbound_tag=:tag)
            AND NOT EXISTS (SELECT 1 FROM allowed_inbounds_association a
                            WHERE a.proxy_id=p.id AND a.inbound_tag=:tag)
        """), params)


def downgrade():
    conn = op.get_bind()
    for tag, protocol in active_inbounds():
        params = {"tag": tag, "protocol": protocol}
        conn.execute(sa.text("DELETE FROM exclude_inbounds_association WHERE inbound_tag=:tag"), params)
        conn.execute(sa.text("""
            INSERT INTO exclude_inbounds_association(proxy_id,inbound_tag)
            SELECT p.id,:tag FROM proxies p WHERE lower(p.type)=:protocol
            AND NOT EXISTS (SELECT 1 FROM allowed_inbounds_association a
                            WHERE a.proxy_id=p.id AND a.inbound_tag=:tag)
        """), params)
    op.drop_table("allowed_inbounds_association")
