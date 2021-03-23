#
# Group Chat Server
#
# Copyright (C) 2019 Internet Real-Time Laboratory
#
# Written by Jan Janak <janakj@cs.columbia.edu>
#
import re
import os
import shlex
import types
import time
import json
import sqlite3
import traceback
import pjsua2 as pj
import time
from   types import SimpleNamespace
from   email.utils import formatdate, parseaddr


DOMAIN         = os.environ['DOMAIN']
ADMIN_PASSWORD = os.environ['ADMIN_PASSWORD']
OUTBOUND_PROXY = os.environ.get('OUTBOUND_PROXY', 'sip:127.0.0.1:5060;transport=tcp')
REGISTRAR      = os.environ.get('REGISTRAR', 'sip:%s' % DOMAIN)
CMD_MARKER     = os.environ.get('CMD_MARKER', '#')
DEBUG          = os.environ.get('DEBUG', False)
LISTEN         = os.environ.get('LISTEN', '127.0.0.1:0')
ROBOT          = os.environ.get('ROBOT', '"Chat Robot" <sip:chatrobot@%s>' % DOMAIN)
DB_FILE        = os.environ.get('DB_FILE', '/data/chat.db')


class ChatException(Exception):         pass
class RoomConflict(ChatException):      pass
class RoomMemberConflict(RoomConflict): pass
class RoomOwnerConflict(RoomConflict):  pass

class AbortCommand(ChatException):
    def __init__(self, msg, code, reason):
        super().__init__(msg)
        self.code = code
        self.reason = reason

class BadRequest(AbortCommand):
    def __init__(self, msg, code=400, reason='Bad Request'):
        super().__init__(msg, code=code, reason=reason)

class Forbidden(AbortCommand):
    def __init__(self, msg, code=403, reason='Forbidden'):
        super().__init__(msg, code=code, reason=reason)

class NotFound(AbortCommand):
    def __init__(self, msg, code=404, reason='Not Found'):
        super().__init__(msg, code=code, reason=reason)

class Conflict(AbortCommand):
    def __init__(self, msg, code=409, reason='Conflict'):
        super().__init__(msg, code=code, reason=reason)


def debug(msg):
    if DEBUG is not False:
        print(msg)


def str2bool(v):
    t = v.lower()
    if t in ('yes', 'true', 't', '1', 'on'):
        return True
    elif t in ('no', 'false', 'f', '0', 'off'):
        return False
    else:
        raise ValueError('invalid parameter value %s' % v)


def URI(v):
    i = v.find('<')
    if i != -1:
        v = v[i+1:]
        i = v.find('>')
        v = v[:i]

    i = v.find('@')
    if i != -1:
        return v.lower()

    return 'sip:%s@%s' % (v.lower(), DOMAIN.lower())


def Username(v):
    i = v.find('<')
    if i != -1:
        v = v[i+1:]
        i = v.find('>')
        v = v[:i]

    i = v.find(':')
    j = v.find('@')
    return v[i+1:j]


# Returns a name-addr from the following formats:
# - username
# - sip:username@domain
# - display_name <sip:username@domain>
#
def RoomID(room_id):
    name, uri = parseaddr(room_id)
    return NameAddr(name or None, URI(uri))


def NameAddr(name, uri):
    uri = URI(uri)
    return '"%s" <%s>' % (name, uri) if name else '<%s>' % uri


def DisplayName(naddr):
    name, _ = parseaddr(naddr)
    return name or None


def FriendlyName(naddr):
    return DisplayName(naddr) or Username(naddr)



def RequestID(msg):
    m = re.search(r'^X-Request-ID:.*$', msg, flags=re.IGNORECASE | re.MULTILINE)
    if m is None:
        m = re.search(r'^Call-ID:.*$', msg, flags=re.IGNORECASE | re.MULTILINE)

    if m is not None:
        return ':'.join(msg[m.start():m.end()].split(':')[1:]).strip()


def get_account(naddr):
    try:
        acc = accounts[URI(naddr)]
    except KeyError:
        acc = RoomAccount(naddr, register=False)
        accounts[acc.uri] = acc
    return acc


def append_header(imp, name, value):
    h = pj.SipHeader()
    h.hName = name
    h.hValue = value
    imp.txOption.headers.append(h)


class Outbox(object):
    def __init__(self):
        self.pushers = dict()
        self.flush()

    def _key(self, dst, src):
        return '%s%s' % (URI(dst), URI(src))

    def flush(self):
        rows = db.execute('SELECT DISTINCT to_uri, from_uri FROM outbox').fetchall()
        if len(rows):
            debug('Flushing outbox (%d messages)' % len(rows))

        for to_uri, from_uri in rows:
            key = self._key(to_uri, from_uri)
            try:
                p = self.pushers[key]
            except KeyError:
                debug('[%s] Creating pusher task for [%s]' % (Username(from_uri), Username(to_uri)))
                p = Pusher(to_uri, from_uri)
                self.pushers[key] = p
            p.run()

    def enqueue(self, to, from_, msg, hdr=None, flush=True):
        to_uri = URI(to)
        if to_uri != to and '<%s>' % to_uri != to:
            hdr = hdr or dict()
            hdr['To'] = to

        from_uri = URI(from_)
        if from_uri != from_ and '<%s>' % from_uri != from_:
            hdr = hdr or dict()
            hdr['From'] = from_

        if hdr is not None:
            hdr = json.dumps(hdr)

        db.execute('INSERT INTO outbox (to_uri, from_uri, message, headers) VALUES (?, ?, ?, ?)',
                   (to_uri, from_uri, msg, hdr))
        db.commit()
        if flush:
            self.flush()


class Pusher(object):
    def __init__(self, to_uri, from_uri):
        self.to_uri = to_uri
        self.from_uri = from_uri
        self._running = False

    def get_buddy(self):
        acc = get_account(self.from_uri)
        return acc.get_buddy(self.to_uri)

    def run(self):
        if self._running:
            return
        self._running = True

        with db:
            row = db.execute('''
                  SELECT id, message as msg, headers as hdr, attempts_made
                    FROM outbox
                   WHERE to_uri=? AND from_uri=?
                ORDER BY enqueued
                   LIMIT 1''', (self.to_uri, self.from_uri)).fetchone()
            if row is None:
                self._running = False
                return

            self.msgid = row['id']
            hdr = json.loads(row['hdr']) if row['hdr'] else {}
            buddy = self.get_buddy()

            debug('[%s][%s] Pushing messsage %d' % (Username(self.from_uri), Username(self.to_uri), self.msgid))
            buddy.send(row['msg'], callback=self._callback, headers=hdr)
            db.execute('UPDATE outbox SET attempts_made=? WHERE id=?', (row['attempts_made'] + 1, self.msgid))

    def _callback(self, code, reason):
        self._running = False
        debug('[%s][%s] Message %d: %d %s' % (Username(self.from_uri), Username(self.to_uri), self.msgid, code, reason))

        with db:
            if code >= 200 and code <= 299:
                debug('[%s][%s] Deleting message %d from outbox' % (Username(self.from_uri), Username(self.to_uri), self.msgid))
                db.execute('DELETE FROM outbox WHERE id=?', (self.msgid,))
            else:
                db.execute('UPDATE outbox SET status_code=?, status_reason=? WHERE id=?', (code, reason, self.msgid))
        self.run()


class Buddy(pj.Buddy):
    def __init__(self, naddr, account):
        super().__init__()
        self._account = account
        self.create(account, self._configure(naddr))
        self._running = False
        buddies[self._key] = self

    def _configure(self, naddr):
        self._cfg = pj.BuddyConfig()
        self._cfg.uri = naddr
        self._cfg.subscribe = False
        return self._cfg

    @property
    def uri(self):
        return URI(self._cfg.uri)

    @property
    def _key(self):
        return '%s%s' % (self.uri, self._account.uri)

    def send(self, msg, callback=None, headers={}):
        if self._running:
            raise AssertionError('Buddy %s is busy' % self.uri)

        def _cb(code, reason):
            self._running = False
            if callback:
                callback(code, reason)

        trampoline[self._key] = _cb

        imp = pj.SendInstantMessageParam()
        for name, value in headers.items():
            if name.lower() in ['f', 't', 'from', 'to']:
                continue
            append_header(imp, name, value)

        imp.content = str(msg)
        self._running = True
        self.sendInstantMessage(imp)


class Room(object):
    def __init__(self, id, uri, owner, name=None, subject=None, private=False, created=None, members=set()):
        self.id = id
        self.uri = URI(uri)
        self.owner = URI(owner)
        self.name = name
        self.subject = subject
        self.private = private
        self.created = created
        self.members = {URI(m): m for m in members}
        self.account = get_account(self.uri)

    def __contains__(self, naddr):
        return URI(naddr) in self.members

    @property
    def From(self):
        return NameAddr(self.name, self.uri)

    @classmethod
    def enum(cls):
        c = db.execute('SELECT name, uri FROM room')
        return set([NameAddr(v['name'], v['uri']) for v in c.fetchall()])

    @classmethod
    def load(cls, room_id):
        rows = db.execute('''
            SELECT r.*, m.room as mroom, m.name as mname, m.uri as muri
             FROM room r LEFT JOIN member m
               ON r.id=m.room
            WHERE r.uri=?''', (URI(room_id),)).fetchall()
        if not rows:
            return None
        row = rows[0]

        m = [NameAddr(r['mname'], r['muri']) for r in rows if r['mroom'] is not None]
        return Room(row['id'], row['uri'], row['owner'],
                    name=row['name'], subject=row['subject'],
                    private=(row['private'] != 0), created=row['created'], members=m)

    @classmethod
    def create(cls, naddr, owner, *members, private=False):
        uri = URI(naddr)
        members = {URI(m): m for m in members}
        with db:
            c = db.execute('INSERT INTO room (uri, owner, name, private) VALUES (?, ?, ?, ?)',
                (uri, URI(owner), DisplayName(naddr), private))
            db.executemany('INSERT INTO member (room, name, uri) VALUES (?, ?, ?)',
                [(c.lastrowid, DisplayName(naddr), uri) for uri, naddr in members.items()])
        get_account(naddr).register(True)

    def isOwner(self, naddr):
        return self.owner == URI(naddr)

    def _sync_members(self):
        with db:
            db.execute('DELETE FROM member WHERE room=?', (self.id,))
            db.executemany('INSERT INTO member (room, name, uri) VALUES (?, ?, ?)',
                [(self.id, DisplayName(m), URI(m)) for m in self.members.values()])

    def add(self, *naddrs):
        old = len(self.members.keys())
        self.members.update({URI(m): m for m in naddrs})
        new = len(self.members.keys())
        self._sync_members()
        return new - old

    def remove(self, *naddrs):
        old = len(self.members.keys())
        for m in map(URI, naddrs):
            try:
                del self.members[m]
            except KeyError:
                pass
        new = len(self.members.keys())
        self._sync_members()
        return old - new

    # FIXME: This could be implemented more intelligently via setters/getters
    def set(self, key, value):
        k = key.lower()
        if   k == 'name':
            self.name = value
        elif k == 'owner':
            if value is None:
                raise ValueError('value must be owner uri')
            self.owner = URI(owner)
        elif k == 'private':
            if value is None:
                raise ValueError('value must be true/false')
            self.private = str2bool(value)
        elif k == 'subject':
            self.subject = value
        else:
            raise ValueError('unsupported parameter %s' % key)
        self.save()

    def get(self, key):
        k = key.lower()
        if   k == 'name':    return self.name
        elif k == 'owner':   return self.owner
        elif k == 'private': return self.private
        elif k == 'subject': return self.subject
        else:
            raise ValueError('unsupported parameter %s' % key)

    def save(self):
        db.execute('UPDATE room SET name=?, subject=?, owner=?, private=? WHERE id=?', (self.name, self.subject, self.owner, self.private, self.id))
        db.commit()
        self.account.reconfigure(NameAddr(self.name, self.uri))

    def destroy(self):
        self.account.register(False)
        db.execute('DELETE FROM room WHERE id=?', (self.id,))
        db.commit()

    def broadcast(self, message, sender=None, skip_sender=True):
        if sender is not None:
            message = '%s: %s' % (FriendlyName(sender), message)

        db.execute('INSERT INTO history (room, message) VALUES (?, ?)', (self.id, message))
        db.commit()

        sender_uri = URI(sender) if sender else None

        hdr = {'Subject': self.subject} if self.subject else {}

        for uri, naddr in self.members.items():
            if skip_sender and uri == sender_uri:
                continue
            self.account.send(naddr, message, hdr=hdr, flush=False)

        outbox.flush()


class Account(pj.Account):
    def __init__(self, naddr, make_default=False, register=False):
        super().__init__()
        self._register = register
        self.create(self._configure(naddr, register), make_default=make_default)
        accounts[self.uri] = self

    def _configure(self, naddr, register):
        self._cfg = pj.AccountConfig()
        self._cfg.idUri = naddr
        self._cfg.sipConfig.proxies.push_back(OUTBOUND_PROXY)
        self._cfg.regConfig.registrarUri = REGISTRAR
        self._cfg.regConfig.retryIntervalSec = 5
        self._cfg.regConfig.registerOnAdd = register
        return self._cfg

    def reconfigure(self, naddr):
        try:
            del accounts[self.uri]
        except KeyError:
            pass

        self.modify(self._configure(naddr, self._register))
        accounts[self.uri] = self

    def register(self, state):
        self._register = state
        try:
            self.setRegistration(state)
        except pj.Error:
            pass

    @property
    def uri(self):
        return URI(self._cfg.idUri)

    @property
    def From(self):
        return self._cfg.idUri

    def get_buddy(self, naddr):
        try:
            return self.findBuddy(naddr)
        except pj.Error:
            return Buddy(naddr, self)

    # FIXME: This method isn't really necessary, it would be better to
    # create a method to just assemble headers and let the caller
    # invoke outbox.enqueue.
    def send(self, naddr, msg, hdr=dict(), flush=True):
        hdr = dict(hdr)
        hdr['Supported'] = 'x-chat-api'
        hdr['Date'] = formatdate(timeval=None, usegmt=True)

        if request:
            irt = RequestID(request.Message)
            if irt is not None:
                hdr['In-Reply-To'] = irt

        if not isinstance(msg, str):
            msg = json.dumps(msg)

        outbox.enqueue(naddr, self.From, msg, hdr, flush=flush)

    #FIXME: Find a way to set subject on the reply message
    def reply(self, msg, code=200, reason='OK'):
        self.send(request.From, msg, hdr={'X-Request-Status': '%d %s' % (code, reason)})

    def onInstantMessageStatus(self, prm):
        try:
            key = '%s%s' % (prm.toUri, self.uri)
            f = trampoline[key]
            del trampoline[key]
            f(prm.code, prm.reason)
        except KeyError:
            pass

    def onInstantMessage(self, prm):
        global request

        request = SimpleNamespace()
        try:
            request.From = prm.fromUri
            request.To = prm.toUri
            request.Body = prm.msgBody
            request.Message = prm.rdata.wholeMsg

            try:
                return self.dispatch(request.Body)
            except AbortCommand as e:
                self.reply('Error: %s' % str(e), code=e.code, reason=e.reason)
            except Exception:
                self.reply(traceback.format_exc() if DEBUG else 'Server Error', code=500, reason='Server Error')
        finally:
            request = None

    def dispatch_command(self, msg):
        self.argv = shlex.split(msg[len(CMD_MARKER):])

        if len(self.argv) < 1:
            raise BadRequest('missing command')

        self.argv[0] = self.argv[0].lower()

        try:
            f = getattr(self, 'cmd_' + self.argv[0])
        except AttributeError:
            raise BadRequest('unknown command')

        try:
            f(*self.argv[1:])
        # FIXME: Do not rely on TypeError for argument checking. The
        # exception can propagate from a place deep inside the
        # function code.
        except TypeError:
            raise BadRequest('invalid arguments (try %shelp %s)' % (CMD_MARKER, self.argv[0]))

    def load_room(self, room_id):
        room = Room.load(room_id)
        if not room:
            raise NotFound('room not found')
        return room

    def cmd_help(self):
        self.reply('Help is on the way')

    def cmd_describe_js(self, room_id):
        room = self.load_room(room_id)
        self.reply({
            'uri': room.uri,
            'owner': room.owner,
            'name': room.name,
            'private': room.private,
            'subject': room.subject,
            'created': room.created,
            'members': list(room.members.values())
        })

    def cmd_members(self, room_id):
        room = self.load_room(room_id)
        lst = sorted([Username(m) for m in room.members])
        if len(lst):
            self.reply(' '.join(lst))
        else:
            self.reply('(no members)', code=204, reason='No Members')

    def cmd_destroy(self, room_id, sender):
        room = self.load_room(room_id)
        if not room.isOwner(sender):
            raise Forbidden('not your room')

        room.destroy()
        self.reply("Room was destroyed")

    def cmd_add(self, room_id, sender, *naddrs):
        room = self.load_room(room_id)
        if not room.isOwner(sender):
            raise Forbidden('only room owner can add others')

        uris = set(map(URI, naddrs))
        suffix = 's' if len(uris) > 1 else ''

        added = room.add(*naddrs)
        if not added:           self.reply('Already member%s' % suffix, code=205)
        elif added < len(uris): self.reply('Some user%s added' % suffix, code=206)
        else:                   self.reply('User%s added' % suffix)

    def cmd_remove(self, room_id, sender, *naddrs):
        room = self.load_room(room_id)
        if room.private:
            if not room.isOwner(sender):
                raise Forbidden('private room and you are not owner')
        else:
            if not room.isOwner(sender):
                s = URI(sender)
                rm = [m for m in naddrs if URI(m) != s]
                if len(rm):
                    raise Forbidden('only owner can remove others')

        uris = set(map(URI, naddrs))
        suffix = 's' if len(uris) > 1 else ''

        removed = room.remove(*naddrs)
        if not removed:           self.reply('Not member%s' % suffix, code=205)
        elif removed < len(uris): self.reply('Some user%s removed' % suffix, code=206)
        else:                     self.reply('User%s removed' % suffix)

    def cmd_set(self, room_id, sender, name, *values):
        room = self.load_room(room_id)
        if not room.isOwner(sender):
            raise Forbidden('only room owner can do that')
        try:
            room.set(name, ' '.join(values) if len(values) else None)
            rv = room.get(name)
            if rv is None:
                self.reply('(empty)', code=204, reason='No Value')
            else:
                self.reply(rv)
        except ValueError as e:
            raise BadRequest(str(e))

    def cmd_get(self, room_id, name):
        room = self.load_room(room_id)
        try:
            rv = room.get(name)
            if rv is None:
                self.reply('(empty)', code=204, reason='No Value')
            else:
                self.reply(rv)
        except ValueError as e:
            raise BadRequest(str(e))


class RobotAccount(Account):
    def __init__(self, naddr=ROBOT, make_default=True, register=True):
        super().__init__(naddr, register=register, make_default=make_default)

    def cmd_rooms(self):
        lst = sorted([Username(v) for v in Room.enum()])
        if len(lst):
            self.reply(' '.join(lst))
        else:
            self.reply('(no rooms)', code=204, reason='No Rooms')

    def cmd_rooms_js(self):
        self.reply(list(map(dict, db.execute('SELECT * FROM room').fetchall())))

    def cmd_create(self, room, *members):
        try:
            Room.create(RoomID(room), request.From, *members)
        except sqlite3.IntegrityError:
            raise Conflict('room already exists')
        self.reply("Room was created")

    def cmd_describe_js(self, room):        super().cmd_describe_js(room)
    def cmd_destroy(self, room):            super().cmd_destroy(room, request.From)
    def cmd_add(self, room, *members):      super().cmd_add(room, request.From, *members)
    def cmd_remove(self, room, *members):   super().cmd_remove(room, request.From, *members)
    def cmd_set(self, room, name, *values): super().cmd_set(room, request.From, name, *values)
    def cmd_get(self, room, name):          super().cmd_get(room, name)

    def dispatch(self, msg):
        if not msg.startswith(CMD_MARKER):
            raise BadRequest('invalid command')

        self.dispatch_command(msg)



class RoomAccount(Account):
    def cmd_describe_js(self):        super().cmd_describe_js(request.To)
    def cmd_members(self):            super().cmd_members(request.To)
    def cmd_destroy(self):            super().cmd_destroy(request.To, request.From)
    def cmd_add(self, *members):      super().cmd_add(request.To, request.From, *members)
    def cmd_remove(self, *members):   super().cmd_remove(request.To, request.From, *members)
    def cmd_set(self, name, *values): super().cmd_set(request.To, request.From, name, *values)
    def cmd_get(self, name):          super().cmd_get(request.To, name)

    def cmd_join(self):
        room = self.load_room(request.To)

        if request.From in room:
            self.reply('You are member already', code=201)
        else:
            if room.private:
                raise Forbidden('cannot join private room')

            room.add(request.From)
            self.reply("You have joined the room")

    def cmd_leave(self):
        room = self.load_room(request.To)

        if request.From not in room:
            self.reply('You are not member', code=201)
        else:
            if room.private:
                raise Forbidden('cannot leave private room')

            room.remove(request.From)
            self.reply("You have left the room")

    def dispatch(self, msg):
        room = self.load_room(self.uri)
        if room.private:
            if request.From not in room and not room.isOwner(request.From):
                raise Forbidden('access denied')

        if msg.startswith(CMD_MARKER):
            self.dispatch_command(msg)
        else:
            room.broadcast(msg, sender=request.From)



def create_database():
    global db

    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')

    # If the AUTOINCREMENT keyword appears after INTEGER PRIMARY KEY,
    # that changes the automatic ROWID assignment algorithm to prevent
    # the reuse of ROWIDs over the lifetime of the database. In other
    # words, the purpose of AUTOINCREMENT is to prevent the reuse of
    # ROWIDs from previously deleted rows.
    db.executescript('''
CREATE TABLE IF NOT EXISTS room (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  name    TEXT,
  uri     TEXT    UNIQUE NOT NULL,
  owner   TEXT    NOT NULL,
  subject TEXT,
  private INTEGER(1) NOT NULL DEFAULT 0,
  created INTEGER(4) NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS room_index ON room(uri);

CREATE TABLE IF NOT EXISTS member (
  room     INTEGER    REFERENCES room(id) ON DELETE CASCADE ON UPDATE CASCADE,
  name     TEXT,
  uri      TEXT       NOT NULL,
  added    INTEGER(4) NOT NULL DEFAULT (strftime('%s','now')),
  PRIMARY KEY(room, uri)
);

CREATE INDEX IF NOT EXISTS member_index ON member(room);

CREATE TABLE IF NOT EXISTS history (
  room    INTEGER    REFERENCES room(id) ON DELETE CASCADE ON UPDATE CASCADE,
  sent    INTEGER(4) NOT NULL DEFAULT (strftime('%s','now')),
  message TEXT       NOT NULL
);

CREATE INDEX IF NOT EXISTS room_history ON history(room);

CREATE TABLE IF NOT EXISTS outbox (
  id            INTEGER    PRIMARY KEY,
  to_uri        TEXT       NOT NULL,
  from_uri      TEXT       NOT NULL,
  enqueued      INTEGER(4) NOT NULL DEFAULT (strftime('%s','now')),
  headers       TEXT,
  message       TEXT       NOT NULL,
  attempts_made INTEGER(4) NOT NULL DEFAULT 0,
  status_code   INTEGER(2),
  status_reason TEXT
);

CREATE INDEX IF NOT EXISTS outbox_to ON outbox(to_uri);
CREATE INDEX IF NOT EXISTS outbox_tofrom ON outbox(to_uri, from_uri);
''')
    db.commit()


class UserAgent(pj.Endpoint):
    TIMER_INTERVAL = 5 * 1000
    EVENT_INTERVAL = 100

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.libCreate()

        self.ec = pj.EpConfig()
        self.ec.uaConfig.threadCnt = 0
        self.ec.uaConfig.mainThreadOnly = True

        self.ec.logConfig.consoleLevel = 4

        self.libInit(self.ec)

        self.tc = pj.TransportConfig()
        ip, port = LISTEN.split(':')
        self.tc.boundAddress = ip
        self.tc.port = int(port)
        self.transportCreate(pj.PJSIP_TRANSPORT_TCP, self.tc)

        self.libStart()

    def onTimer(self, prm):
        global outbox
        if outbox is not None:
            outbox.flush()
        self.utilTimerSchedule(self.TIMER_INTERVAL, None)

    def run(self):
        self.utilTimerSchedule(self.TIMER_INTERVAL, None)
        while True:
            self.libHandleEvents(self.EVENT_INTERVAL)

    def cleanup(self):
        self.libDestroy()


if __name__ == "__main__":
    # Global registries of pjsip accounts and budies so that they do
    # not get garbage collected by Python while the native library
    # still holds references to those.
    accounts = dict()
    buddies = dict()
    trampoline = dict()
    outbox = None
    request = None

    create_database()
    try:
        ua = UserAgent()

        RobotAccount()

        for naddr in Room.enum():
            RoomAccount(naddr, register=True)

        outbox = Outbox()

        ua.run()
    finally:
        ua.cleanup()
