import requests
import attr
from attr.validators import instance_of

from .workers.http_worker import WebSource
from .parsers import HTMLParser
from .workers.store_worker import StoreWorker
from .helpers import selector_converter, attr_dict, astuple


@attr.s
class BaseModel(object):
    def _replicate(self, **kwargs):
        return self.__class__(**kwargs)

    def __call__(self, **kwargs):
        return self.__class__(**{**self.__dict__, **kwargs})  # noqa

@attr.s
class Run:
    sources = attr.ib(default=attr.Factory(list))
    templates = attr.ib(default=attr.Factory(list))
    repeat = attr.ib(default=False)
    parser = attr.ib(default=HTMLParser)
    source_worker = attr.ib(default=WebSource)
    n_workers = attr.ib(default=1)
    active = attr.ib(default=True)

@attr.s
class Source(BaseModel):
    url = attr.ib('')
    active = attr.ib(True)
    attrs = attr.ib(attr.Factory(dict), convert=attr_dict, metadata={'Attr': 1})
    method = attr.ib('get')
    parse = attr.ib(True)
    headers = attr.ib(attr.Factory(dict))
    data = attr.ib(attr.Factory(dict))
    params = attr.ib(attr.Factory(dict))
    src_template = attr.ib('{}')
    retries = attr.ib(10)
    json_key = attr.ib(None, convert=astuple)
    duplicate = attr.ib(False)
    copy_attrs = attr.ib(None, convert=astuple)
    attr_condition = attr.ib('')
    parent = attr.ib(False)


def source_conv(source):
    if source:
        if source.__class__ == Source:
            return source
        return Source(**source) if type(source) == dict else Source()


@attr.s
class Attr(BaseModel):
    '''
    An attr is used to hold a value as an attribute for a template.
    The value for the attribute is obtained by applying the func
    on the element obtained through the selector.
    '''
    selector = attr.ib(default=None, convert=astuple)
    name = attr.ib(default=None)
    value = attr.ib(default=None)
    func = attr.ib(default=tuple, convert=astuple,
                   metadata={Run.parser: 1})
    attr_condition = attr.ib(default={})
    source_condition = attr.ib(default={})
    source = attr.ib(default=None, convert=source_conv,
                     metadata={'Source': 1})
    kws = attr.ib(default=attr.Factory(dict), convert=astuple)
    type = attr.ib(default=None)


@attr.s
class Template(BaseModel):
    db = attr.ib(default=None)
    table = attr.ib(default=None)
    db_type = attr.ib(default=None)
    js_regex = attr.ib(default=None)
    objects = attr.ib(init=False)
    source = attr.ib(default=None, convert=source_conv)
    func = attr.ib(default='update', metadata={StoreWorker: 1})
    kws = attr.ib(default=attr.Factory(dict))
    name = attr.ib(default='')
    partial = attr.ib(default=False)
    required = attr.ib(default=False)
    selector = attr.ib(default=None, convert=astuple)
    args = attr.ib(default=tuple)
    attrs = attr.ib(default=attr.Factory(dict), convert=attr_dict)
    url = attr.ib(default='')
    preview = attr.ib(default=False)

    def to_dict(self):
        return {'url': self.url, **self.attrs_to_dict()} # noqa

    def attrs_to_dict(self):
        return {attr.name: attr.value for attr in self.attrs.values()}

    def to_store(self):
        replica = self.__class__(db=self.db, table=self.table,
                                 db_type=self.db_type, kws=self.kws,
                                 name=self.name, url=self.url)
        replica.objects = self.objects[:]
        return replica

    def attrs_from_dict(self, attr_dict):
        self.attrs = {name: Attr(name=name, value=value) for
                      name, value in attr_dict.items()}

    def add_attr(self, attr):
        print('add_attr', attr.name)
        attr = Attr(name=name, value=value, **kwargs)
        self.attrs[attr.name] = attr


class ScrapeModel:
    def __init__(self, name='', domain='', runs: Run=[], num_getters=1, time_out=1, user_agent=None,
                 session=requests.Session(), awaiting=False, cookies={}, db=[], **kwargs):
        self.name = name
        self.domain = domain
        self.runs = runs
        self.num_getters = num_getters
        self.time_out = time_out
        self.session = session
        self.awaiting = awaiting
        self.user_agent = user_agent
        self.db = db

        if cookies:
            print(cookies)
            requests.utils.add_dict_to_cookiejar(self.session.cookies, cookies)

        for key, value in kwargs.items():
            setattr(self, key, value)
