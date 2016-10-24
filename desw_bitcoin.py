"""
Plugin for bitcoind over RPC.
This module can be imported by desw and used like a plugin.
It also is meant to be called from the command line.
To configure for use with bitcoind, call this file from
walletnotify and blocknotify.
"""

import argparse
import json
import sys
from pycoin.key.validate import is_address_valid
from desw import CFG, ses, logger, process_credit, confirm_send
from sqlalchemy_models import wallet as wm

from bitcoinrpc.authproxy import AuthServiceProxy
NETCODES = ['BTC', 'XTN']
NETWORK = 'Bitcoin'
CURRENCIES = json.loads(CFG.get(NETWORK.lower(), 'CURRENCIES'))
CONFS = int(CFG.get(NETWORK.lower(), 'CONFS'))
#FEE = int(CFG.get(NETWORK.lower(), 'FEE'))


def create_client():
    """
    Create an RPC client.

    :rtype: AuthServiceProxy
    """
    return AuthServiceProxy(CFG.get(NETWORK.lower(), 'RPCURL'))


def get_new_address():
    """
    Get a new address from the client.

    :rtype: str
    """
    client = create_client()
    return str(client.getnewaddress())


def validate_address(address, network=None):
    """
    Validate an address of the given network.

    :param str address: The address to validate
    :param str network: The network the address belongs to (i.e. BTC)
    :rtype: bool
    """

    try:
        netcode = is_address_valid(address, allowable_netcodes=NETCODES)
    except Exception:
        return False
    if netcode is None or (network is not None and netcode != network):
        return False
    return True


def send_to_address(address, amount):
    """
    Send the amount of coins to the address indicated.

    :param str address: The address to send to
    :param float amount: The amount of coins to send as a float
    :return: the transaction id (txid)
    :rtype: str
    """
    client = create_client()
    txid = str(client.sendtoaddress(address, amount))
    adjust_hwbalance(available=-amount, total=-amount)
    return txid


def get_balance():
    """
    Get the wallet's balance. Returns a dict with 'available' and 'total'
    balances, indicating what can be spent right now, and what is the total
    including unconfirmed funds.

    :rtype: dict
    """
    hwb = ses.query(wm.HWBalance).filter(wm.HWBalance.network == NETWORK.lower()).order_by(wm.HWBalance.time.desc()).first()
    return {'total': hwb.total, 'available': hwb.available}


def process_receive(txid, details, confirmed=False):
    """
    Process an incoming transaction with the given txid and details.
    If valid and new, create a Credit and update the corresponding Balance.

    :param str txid: The txid for the transaction in question
    :param dict details: The transaction details as returned by rpc client.
    :param bool confirmed: Has this transaction received enough confirmations?
    """
    creds = ses.query(wm.Credit).filter(wm.Credit.ref_id == txid)
    if creds.count() > 0:
        logger.info("txid already known. returning.")
        return
    transaction_state = 'complete' if confirmed else 'unconfirmed'
    addy = ses.query(wm.Address)\
        .filter(wm.Address.address == details['address']).first()
    if not addy:
        logger.warning("address not known. returning.")
        return
    amount = details['amount']
    logger.info("crediting txid %s" % txid)
    process_credit(amount=amount, address=details['address'],
                   currency=CURRENCIES[0], network=NETWORK, transaction_state=transaction_state,
                   reference='tx received', ref_id=txid,
                   user_id=addy.user_id)
    adjust_hwbalance(available=None, total=amount)


def adjust_hwbalance(available=None, total=None):
    if available is None and total is None:
        return
    hwb = ses.query(wm.HWBalance).filter(wm.HWBalance.network == NETWORK.lower()).order_by(wm.HWBalance.time.desc()).first()
    if available is not None:
        hwb.available += available
    if total is not None:
        hwb.total += total
    ses.add(hwb)
    try:
        ses.commit()
    except Exception as e:
        logger.exception(e)
        ses.rollback()
        ses.flush()


lastblock = 0

def main(sys_args=sys.argv[1:]):
    """
    The main CLI entry point. Reads the command line arguments which should
    be filled in by the calling wallet node. Handler for walletnotify and
    blocknotify.
    """
    global lastblock
    client = create_client()
    parser = argparse.ArgumentParser()
    parser.add_argument("type")
    parser.add_argument("data")
    args = parser.parse_args(sys_args)
    typ = args.type
    if typ == 'transaction' and args.data is not None:
        txid = args.data
        txd = client.gettransaction(txid)
        confirmed = txd['confirmations'] >= CONFS
        for p, put in enumerate(txd['details']):
            if put['category'] == 'send':
                confirm_send(put['address'], put['amount'],
                             ref_id="%s:%s" % (txid, p))
            elif put['category'] == 'receive':
                process_receive("%s:%s" % (txid, p), put, confirmed)

    elif typ == 'block':
        info = client.getinfo()
        if info['blocks'] <= lastblock:
            return
        lastblock = info['blocks']
        creds = ses.query(wm.Credit)\
            .filter(wm.Credit.transaction_state == 'unconfirmed')\
            .filter(wm.Credit.network == NETWORK)
        for cred in creds:
            txid = cred.ref_id.split(':')[0] or cred.ref_id
            txd = client.gettransaction(txid)
            if txd['confirmations'] >= CONFS:
                cred.transaction_state = 'complete'
                for p, put in enumerate(txd['details']):
                    cred.ref_id = "%s:%s" % (txd['txid'], p)
                ses.add(cred)
        try:
            ses.commit()
        except Exception as e:
            logger.exception(e)
            ses.rollback()
            ses.flush()

        # update balances
        total = client.getbalance("*", 0)
        avail = info['balance']
        hwb = wm.HWBalance(avail, total, CURRENCIES[0], NETWORK.lower())
        ses.add(hwb)
        try:
            ses.commit()
        except Exception as ie:
            ses.rollback()
            ses.flush()


if __name__ == "__main__":
    main()

