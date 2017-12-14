import requests
import attr
from attr.validators import instance_of

from .sources import WebSource
from .parsers import HTMLParser
from .workers.store_worker import StoreWorker
from .helpers import selector_converter, attr_dict, str_as_tuple
from . import databases


@attr.s
class BaseModel(object):
    def _replicate(self, **kwargs):
        return self.__class__(**kwargs)

    def __call__(self, **kwargs):
        return self.__class__(**{**self.__dict__, **kwargs})  # noqa


@attr.s
class Phase:
    active = attr.ib(default=True)
    name = attr.ib(default='')
    n_workers = attr.ib(default=1)
    repeat = attr.ib(default=False)
    sources = attr.ib(default=attr.Factory(list))
    source_worker = attr.ib(default=WebSource)
    synchronize = attr.ib(default=False)
    templates = attr.ib(default=attr.Factory(list))
    parser = attr.ib(default=HTMLParser)

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
    json_key = attr.ib(None, convert=str_as_tuple)
    duplicate = attr.ib(False)
    copy_attrs = attr.ib(None, convert=str_as_tuple)
    attr_condition = attr.ib('')
    parent = attr.ib(False)
    from_db = attr.ib(None, metadata={'Template': 1})


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
    selector = attr.ib(default=None, convert=str_as_tuple)
    name = attr.ib(default=None)
    value = attr.ib(default=None, convert=str_as_tuple)
    func = attr.ib(default=tuple, convert=str_as_tuple,
                   metadata={Phase.parser: 1})
    attr_condition = attr.ib(default={})
    source_condition = attr.ib(default={})
    source = attr.ib(default=None, convert=source_conv,
                     metadata={'Source': 1})
    kws = attr.ib(default=attr.Factory(dict))
    type = attr.ib(default=None)
    value_template = attr.ib(default=None)

    def __attrs_post_init__(self):
        # Ensure that the kws are encapsulated in a list with the same length
        # as the funcs.
        if type(self.kws) not in [list, tuple]:
            self.kws = (self.kws,)

        difference = len(self.func) - len(self.kws)
        if difference:
            for _ in range(difference):
                self.kws.append({})

@attr.s
class Template(BaseModel):
    db = attr.ib(default=None)
    table = attr.ib(default=None)
    db_type = attr.ib(default=None)
    js_regex = attr.ib(default=None)
    objects = attr.ib(init=False)
    source = attr.ib(default=None, convert=source_conv)
    func = attr.ib(default='create', metadata={StoreWorker: 1})
    kws = attr.ib(default=attr.Factory(dict))
    name = attr.ib(default='')
    partial = attr.ib(default=False)
    required = attr.ib(default=False)
    selector = attr.ib(default=None, convert=str_as_tuple)
    args = attr.ib(default=tuple)
    attrs = attr.ib(default=attr.Factory(dict), convert=attr_dict)
    url = attr.ib(default='')
    preview = attr.ib(default=False)

    def __attrs_post_init__(self):
        if self.db_type and not (self.db and self.table):
            raise Exception(self.name +
                'Database type is set, but not the names and the table')
        elif (self.db or self.table) and not self.db_type:
            raise Exception(self.name +
                'Database name and table are set, but not the database type')

        if self.url:
            self.attrs['url'] = Attr(name='url', value=self.url)

    def to_dict(self):
        return {'url': self.url, **self.attrs_to_dict()} # noqa

    def attrs_to_dict(self):
        return {attr.name: attr.value for attr in self.attrs.values()}

    def to_store(self):
        replica = self.__class__(db=self.db, table=self.table, func=self.func,
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
    def __init__(self, name='', domain='', phases: Phase=[], num_getters=1,
                 time_out=1, user_agent=None, session=requests.Session(),
                 awaiting=False, cookies={}, schedule='', **kwargs):
        self.name = name
        self.domain = domain
        self.phases = phases
        self.num_getters = num_getters
        self.time_out = time_out
        self.session = session
        self.awaiting = awaiting
        self.user_agent = user_agent
        self.schedule = schedule

        if cookies:
            print(cookies)
            requests.utils.add_dict_to_cookiejar(self.session.cookies, cookies)

        for key, value in kwargs.items():
            setattr(self, key, value)

    def get_template(self, key):
        for phase in self.phases:
            for template in run.templates:
                if template.name == key:
                    return template

    def read_template(self, template_name='', as_object=False):
        template = self.get_template(template_name)
        database = databases._threads[template.db_type]()
        return database.read(template=template)
