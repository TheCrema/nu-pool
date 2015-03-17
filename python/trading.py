import os
import sys
import time
import json
import urllib
import urllib2
import threading
import subprocess
import logging
import tempfile
import signal
from math import ceil
from utils import *


class NuBot(ConnectionThread):
  def __init__(self, conn, key, secret, exchange, unit, logger = None):
    super(NuBot, self).__init__(conn, logger)
    self.process = None
    self.unit = unit
    self.running = False
    self.exchange = exchange
    self.options = {
      'exchangename' : repr(exchange),
      'apikey' : key,
      'apisecret' : secret,
      'txfee' : 0.2,
      'pair' : 'nbt_' + unit,
      'submit-liquidity' : False,
      'dualside' : True,
      'multiple-custodians' : True,
      'executeorders' : True,
      'mail-notifications' : False,
      'hipchat' : False
    }
    if unit != 'usd':
      if unit == 'btc':
        self.options['secondary-peg-options'] = { 
        'main-feed' : 'bitfinex',
        'backup-feeds' : {  
          'backup1' : { 'name' : 'blockchain' },
          'backup2' : { 'name' : 'coinbase' },
          'backup3' : { 'name' : 'bitstamp' }
        } }
      else:
        self.logger.error('no price feed available for %s', unit)
      self.options['secondary-peg-options']['wallshift-threshold'] = 0.3
      self.options['secondary-peg-options']['spread'] = 0.0

  def run(self):
    out = tempfile.NamedTemporaryFile(delete = False)
    out.write(json.dumps({ 'options' : self.options }))
    out.close()
    while self.active:
      if self.pause:
        self.shutdown()
      elif not self.process:
        with open(os.devnull, 'w') as fp:
          self.logger.info("starting NuBot for unit %s on exchange %s", self.unit, repr(self.exchange))
          self.process = subprocess.Popen("java -jar NuBot.jar %s" % out.name,
            stdout=fp, stderr=fp, shell=True, cwd = 'nubot')
      time.sleep(10)

  def shutdown(self):
    if self.process:
      self.logger.info("stopping NuBot for unit %s on exchange %s", self.unit, repr(self.exchange))
      self.process.terminate()
      #os.killpg(self.process.pid, signal.SIGTERM)
      self.process = None


class PyBot(ConnectionThread):
  def __init__(self, conn, key, secret, exchange, unit, logger = None):
    super(PyBot, self).__init__(conn, logger)
    self.key = key
    self.secret = secret
    self.exchange = exchange
    self.unit = unit
    self.spread = 0.002
    if not hasattr(PyBot, 'lock'):
      PyBot.lock = {}
    if not repr(exchange) in PyBot.lock:
      PyBot.lock[repr(exchange)] = threading.Lock()
    if not hasattr(PyBot, 'pricefeed'):
      PyBot.pricefeed = PriceFeed(30, logger)
    if not hasattr(PyBot, 'interest'):
      PyBot.interest = [0, {}]

  def shutdown(self):
    self.logger.info("stopping PyBot for unit %s on exchange %s", self.unit, repr(self.exchange))
    trials = 0
    while trials < 10:
      try:
        response = self.exchange.cancel_orders(self.unit, self.key, self.secret)
      except:
        response = {'error' : 'exception caught: %s' % sys.exc_info()[1]}
      if 'error' in response:
        self.logger.error('unable to cancel orders for unit %s on exchange %s (trial %d): %s', self.unit, repr(self.exchange), trials + 1, response['error'])
        self.exchange.adjust(response['error'])
        self.logger.info('adjusting nonce of exchange %s to %d', repr(self.exchange), self.exchange._shift)
      else:
        self.logger.info('successfully deleted all orders for unit %s on exchange %s', self.unit, repr(self.exchange))
        break
      trials = trials + 1

  def acquire_lock(self):
    PyBot.lock[repr(self.exchange)].acquire()

  def release_lock(self):
    PyBot.lock[repr(self.exchange)].release()

  def update_interest(self):
    curtime = time.time()
    if curtime - PyBot.interest[0] > 120:
      PyBot.interest[1] = self.conn.get('exchanges', trials = 1)
      PyBot.interest[0] = curtime

  def place(self, side):
    price = self.serverprice
    if side == 'ask':
      exunit = 'nbt'
      price *= (1.0 + self.spread)
    else:
      exunit = self.unit
      price *= (1.0 - self.spread)
    price = ceil(price * 10**8) / float(10**8) # truncate floating point precision after 8th position
    try:
      response = self.exchange.get_balance(exunit, self.key, self.secret)
    except KeyboardInterrupt: raise
    except: response = { 'error' : 'exception caught: %s' % sys.exc_info()[1] }
    if 'error' in response:
      self.logger.error('unable to receive balance for unit %s on exchange %s: %s', exunit, repr(self.exchange), response['error'])
      self.exchange.adjust(response['error'])
    elif response['balance'] > 0.0001:
      balance = response['balance'] if exunit == 'nbt' else response['balance'] / price
      self.update_interest()
      try:
        response = self.exchange.place_order(self.unit, side, self.key, self.secret, balance, price)
      except KeyboardInterrupt: raise
      except: response = { 'error' : 'exception caught: %s' % sys.exc_info()[1] }
      if 'error' in response:
        self.logger.error('unable to place %s %s order of %.4f nbt at %.8f on exchange %s: %s', side, exunit, balance, price, repr(self.exchange), response['error'])
        self.exchange.adjust(response['error'])
      else:
        self.logger.info('successfully placed %s %s order of %.4f nbt at %.8f on exchange %s', side, exunit, balance, price, repr(self.exchange))
    return response

  def reset(self, cancel = True):
    self.acquire_lock()
    response = { 'error' : True }
    while 'error' in response:
      response = {}
      if cancel:
        try: response = self.exchange.cancel_orders(self.unit, self.key, self.secret)
        except KeyboardInterrupt: raise
        except: response = { 'error' : 'exception caught: %s' % sys.exc_info()[1] }
        if 'error' in response:
          self.logger.error('unable to cancel orders for unit %s on exchange %s: %s', self.unit, repr(self.exchange), response['error'])
        else:
          self.logger.info('successfully deleted all orders for unit %s on exchange %s', self.unit, repr(self.exchange))
      if not 'error' in response:
        response = self.place('bid')
        if not 'error' in response:
          response = self.place('ask')
      if 'error' in response:
        if 'exception caught:' in response['error']:
          self.logger.info('retrying in 5 seconds ...')
          time.sleep(5)
        else:
          self.exchange.adjust(response['error'])
          self.logger.info('adjusting nonce of exchange %s to %d', repr(self.exchange), self.exchange._shift)
    self.release_lock()
    return response

  def run(self):
    self.logger.info("starting PyBot for unit %s on exchange %s", self.unit, repr(self.exchange))
    self.update_interest()
    self.serverprice = self.conn.get('price/' + self.unit)['price']
    self.reset() # initialize walls
    prevprice = self.serverprice
    curtime = time.time()
    while self.active:
      time.sleep(max(30 - time.time() + curtime, 0))
      curtime = time.time()
      if self.pause:
        self.shutdown()
      else:
        response = self.conn.get('price/' + self.unit, trials = 3)
        if not 'error' in response:
          self.serverprice = response['price']
          self.update_interest()
          userprice = PyBot.pricefeed.price(self.unit)
          if 1.0 - min(self.serverprice, userprice) / max(self.serverprice, userprice) > 0.005: # validate server price
            self.logger.error('server price %.8f for unit %s deviates too much from price %.8f received from ticker, will delete all orders for this unit', self.serverprice, self.unit, userprice)
            self.shutdown()
          else:
            deviation = 1.0 - min(prevprice, self.serverprice) / max(prevprice, self.serverprice)
            if deviation > 0.00425:
              self.logger.info('price of unit %s moved from %.8f to %.8f, will try to reset orders', self.unit, prevprice, self.serverprice)
              prevprice = self.serverprice
            self.reset(deviation > 0.00425)
        else:
          self.logger.error('unable to retrieve server price: %s', response['message'])
    self.shutdown()