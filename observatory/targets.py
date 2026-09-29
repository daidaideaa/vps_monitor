"""Exact products and permitted Telegram sources; never execute message content."""
TARGETS = (
    dict(id='vmiss-jp-tky-tri-basic', provider='VMISS', product_name='JP.TKY.TRI.Basic',
         host='app.vmiss.com', pid='101', channels=['hostmonit','vmisstz','vmiss_com','vmisscom'],
         aliases=['jp.tky.tri.basic'], product_url='https://app.vmiss.com/store/jp-tokyo-tri'),
    dict(id='greencloud-tokyo-premium-mini', provider='GreenCloud',
         product_name='CN Premium Mini (Tokyo)', host='greencloudvps.com', pid='2213',
         channels=['hostmonit'], aliases=['cn premium optimized plan mini (tokyo)', 'cn.premium.mini (tokyo)'],
         product_url='https://greencloudvps.com/billing/store/cn-premium-optimized'),
    dict(id='dmit-tyo-pro-tiny', provider='DMIT', product_name='TYO.Pro.TINY',
         host='www.dmit.io', pid='138', channels=['vps_spiders','dmitnews'], aliases=['tyo.pro.tiny'],
         product_url='https://www.dmit.io/cart.php?a=add&pid=138'),
    dict(id='gomami-jpn-pulse-nano', provider='GoMami', product_name='JPN.Pulse.Nano',
         host='gomami.io', pid='13', channels=['gcpcn','gomaminetworks'], aliases=['jpn.pulse.nano'],
         product_url='https://gomami.io/cart.php?a=add&pid=13'),
)
BY_ID = {t['id']: t for t in TARGETS}
CHANNELS = sorted({c for t in TARGETS for c in t['channels']})
ADMIN_GROUPS = {'vmisscom','gomaminetworks'}
VENDOR_SOURCES = {'vmiss_com','vmisscom','dmitnews','gomaminetworks'}


def trusted_message(source, sender, admins, forwarded=False, peer_id=None):
    if source not in ADMIN_GROUPS: return True
    return not forwarded and (sender in admins or (peer_id is not None and sender == -1000000000000-peer_id))


def public_target(target):
    return {k: target[k] for k in ('id', 'provider', 'product_name', 'product_url')}
