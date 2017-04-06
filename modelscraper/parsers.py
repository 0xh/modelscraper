import re
import json
from multiprocessing import Process, JoinableQueue
from queue import Empty
from functools import reduce
from datetime import datetime

from pybloom import ScalableBloomFilter
import lxml.html as lxhtml
from scrapely import Scraper


class BaseParser(Process):
    '''
    This class implements the methods:
        _gen_source: generate a source if the template has that specified.
        _add_source: add a source to the current queue or
                     forward it to another run.
        _handle_empty
    which can all be overridden in subclasses.
    I
    '''
    def __init__(self, parent=None, templates=[], **kwargs):
        if not parent:
            raise Exception('No parent or run was specified')
        super(BaseParser, self).__init__()
        self.name = parent.name
        self.domain = parent.domain
        self.templates = templates
        self.in_q = JoinableQueue()
        self.source_q = parent.source_q
        self.store_q = parent.store_q
        self.db = parent.db
        self.seen = ScalableBloomFilter()
        self.forwarded = ScalableBloomFilter()
        self.forward_q = parent.forward_q
        self.to_parse = parent.to_parse
        self.parsed = 0
        self.average = []

        for key, value in kwargs.items():
            setattr(self, key, value)

        # Set all the functions of the attrs to the correct functions of the
        # parser.
        self._set_attr_funcs()

    def run(self):
        while True:
            try:
                source = self.in_q.get(timeout=20)
            except Empty:
                print('timed out parser get')
                if self.parsed.value == self.to_parse.value:
                    with self.parsed.get_lock():
                        self.parsed.value = 0
                    break

            data = source.data
            self.seen.add(source.url)
            try:
                if getattr(self, '_prepare_data', None):
                    data = self._prepare_data(source)

                for template in self.templates:
                    self.new_sources = []
                    extracted = self._extract(data, template)
                    template.objects = list(
                        self._gen_objects(template, extracted, source))

                    if template.preview:
                        print(template.objects[0])

                    if not template.objects and template.required:
                        print(template.selector, 'yielded nothing, quitting.')
                        self._handle_empty()

                    if template.db_type:
                        self.store_q.put(template.to_store())

                    for new_source in self.new_sources:
                        self._gen_source(*new_source)

                    del template.objects

            except Exception as E:
                print('parser error', E)

            with self.parsed.get_lock():
                self.parsed.value += 1

            if self.parsed.value == self.to_parse.value:
                with self.parsed.get_lock():
                    self.parsed.value = 0
                self.in_q.task_done()
                break

            self.in_q.task_done()

    def _gen_objects(self, template, extracted, source):
        '''
        Create objects from parsed data using the functions
        defined in the scrape model. Also calls the functions
        that create the sources from Attrs or Templates (_gen_source,
        _source_from_object).
        '''

        for data in extracted:
            # Create a new objct from the template.
            objct = template._replicate(name=template.name, url=source.url)

            # Set predefined attributes from the source.
            for attr in source.attrs.values():
                objct.attrs[attr.name] = attr()

            no_value = 0

            # Set the attributes.
            for attr in self._gen_attrs(template.attrs.values(), objct, data):
                objct.attrs[attr.name] = attr

                if not attr.value:
                    no_value += 1

            # We want to count how many attrs return None
            # Don't return anything if we have no values for the attributes
            if no_value == len(objct.attrs) - len(source.attrs):
                print('Template {} has failed, attempting to use the fallback'.\
                      format(template.name))
                if getattr(self, '_fallback', None) and False:
                    for objct in self._fallback(template, extracted, source):
                        yield objct
                    continue
                else:
                    print('Template', template.name, 'failed')
                    print(data.text_content())
                    continue

            # Create a new Source from the template if desirable
            if template.source and getattr(self, '_source_from_object', None):
                objct.source = template.source()
                self._source_from_object(objct, source)

            yield objct

    def _gen_attrs(self, attrs, objct, data):
        for attr in attrs:
            if attr.selector:
                elements = attr.selector(data)
            else:
                elements = [data]

            # get the parse functions and recursively apply them.
            parsed = self._apply_funcs(elements, attr.func, attr.kws)
            if attr.type and type(parsed) != attr.type:
                print('Not the same type')

            # Create a request from the attribute if desirable
            if attr.source and parsed:
                self.new_sources.append((objct, attr, parsed))

            yield attr._replicate(name=attr.name, value=parsed)

    def _set_attr_funcs(self):
        for template in self.templates:
            for attr in template.attrs.values():
                attr.func = [getattr(self, f, f) for f in attr.func]
                if type(attr.kws) != list:
                    attr.kws = [attr.kws]


    def _apply_funcs(self, elements, parse_funcs, kws):
        if len(parse_funcs) == 1 and hasattr(parse_funcs, '__iter__'):
            return parse_funcs[0](elements, **kws[0])
        else:
            parsed = parse_funcs[0](elements, **kws[0])
            return self._apply_funcs(parsed, parse_funcs[1:],
                                     kws[1:] if kws else [{}])

    def _gen_source(self, objct, attr, parsed):
        if type(parsed) != list:
            parsed = [parsed]

        for value in parsed:
            # for now only "or" is supported.
            if attr.source_condition and \
                    not any(
                        self._evaluate_condition(objct,
                                                 attr.source_condition)
                    ):
                continue

            new_source = attr.source(
                url=self._apply_src_template(attr.source, value))

            if attr.attr_condition and \
                    self.value_is_new(objct, value, attr.attr_condition):
                    self._add_source(new_source)
            else:
                self._add_source(new_source)

    def value_is_new(self, objct, uri, name):
        db_objct = self.db.read(uri, objct)
        if db_objct and db_objct.attrs.get(name):
            if db_objct.attrs[name].value != objct.attrs[name].value:
                return True
            return False

    def _apply_src_template(self, source, url):
        if source.src_template:
            # use formatting notation in the src_template
            return source.src_template.format(url)
        return url

    def _value(self, parsed, index=None):
        if parsed:
            if len(parsed) == 1:
                return parsed[0]
            return parsed[index] if index else parsed

    def _evaluate_condition(self, objct, condition, **kwargs):
        # TODO add "in", and other possibilities.
        for name, cond in condition.items():
            values = objct.attrs[name].value
            # Wrap the value in a list without for example seperating the
            # characters.
            print(values)
            values = [values] if type(values) != list else values
            for val in values:
                if val and eval(str(val) + cond, {}, {}):
                    yield True
                else:
                    yield False

    def _add_source(self, source):
        if source.url and (source.url not in self.seen or source.duplicate) \
                and source.url not in self.forwarded:
            if source.active:
                with self.to_parse.get_lock():
                    self.to_parse.value += 1
                self.source_q.put(source)
                self.seen.add(source.url)
            else:
                self.forward_q.put(source)
                self.forwarded.add(source.url)

    def _handle_empty(self):
        while not self.in_q.empty():
            try:
                self.in_q.get(False)
            except Empty:
                continue
            self.source_q.task_done()


class HTMLParser(BaseParser):
    '''
    A parser that is able to parse html.
    '''
    def __init__(self, **kwargs):
        super(HTMLParser, self).__init__(**kwargs)
        self.scrapely_parser = None
        for key, value in kwargs.items():
            setattr(self, key, value)

    def _prepare_data(self, source):
        json_key = source.json_key
        data = source.data
        if json_key: # if the data is json, return it straightaway
            json_raw = json.loads(data)
            if hasattr(json_key, '__iter__') and json_key[0] in json_raw:
                data = reduce(dict.get, json_key, json_raw)
            elif type(json_key) == str and json_key in json_raw:
                data = json_raw[json_key]
            else:
                return False
        try:  # Create an HTML object from the returned text.
            data = lxhtml.fromstring(data)
        except ValueError:  # This happens when xml is declared in html.
            data = lxhtml.fromstring('\n'.join(data.split('\n')[1:]))
        except TypeError:
            print(data)
            print('Something weird has been returned by the server.')
        data.make_links_absolute(self.domain)
        return data

    def _extract(self, html, template):
        # We have normal html
        if not template.js_regex:
            if html is not None:
                if template.selector:
                    extracted = template.selector(html)
                else:
                    extracted = [html]
            else:
                extracted = []

        # We want to extract a json_variable from the server
        else:
            regex = re.compile(template.js_regex)
            extracted = []
            # Find all the scripts that match the regex.
            scripts = (regex.findall(s.text_content())[0] for s in
                       html.cssselect('script')
                       if regex.search(s.text_content()))

            # Set selected to the scripts
            for script in scripts:
                extracted.extend(json.loads(script))
        return extracted

    def _source_from_object(self, objct, source):
        # TODO fix that the source object can determine for itself where data
        # or params should be placed in the object.
        new_source = objct.source._replicate()
        attrs = {attr.name: attr.value for attr in objct.attrs.values()
                    if attr.name != 'url'}

        if not getattr(new_source, 'url', None):
            url = objct.attrs.get('url')

            if url and not isinstance(url, list):
                new_source.url = self._apply_src_template(source, url.value)
            else:
                new_source.url = self._apply_src_template(source, source.url)

        if new_source.copy_attrs:
            new_source = self._copy_attrs(objcts, new_source)

        if new_source.parent:
            new_source.attrs['_parent'] = objct.attrs['url']._replicate()

        if new_source.method == 'post':
            new_source.data = {**new_source.data, **attrs} # noqa
        else:
            new_source.params = attrs

        self._add_source(new_source)

    def _copy_attrs(self, objct, source):
        # Copy only the attribute with the key
        if type(source.copy_attrs) == str:
            if objct.attrs.get(source.copy_attrs):
                attr = objct.attrs[copy_attrs]._replicate()
                new_source.attrs[copy_attrs] = attr
            else:
                raise Exception('Could not copy attr', copy_attrs)

        # Copy a list of attributes
        elif hasattr(source.copy_attrs, 'iter'):
            for attr_name in source.copy_attrs:
                attr = objct.attrs.get(attr_name)
                if attr:
                    new_source.attrs[attr_name] = attr
                else:
                    raise Exception('Could not copy all attrs', copy_attrs)

        else: # Copy all the attributes.
            new_source.attrs = {key: attr._replicate() for key, attr in
                                objct.attrs.items()}
        return new_source

    def _fallback(self, template, html, source):
        if not self.scrapely_parser:
            self.scrapely_parser = Scraper()

        html = self.scrapely_parser.HtmlPage(body=html)
        db_objct = self.db.read(uri, objct)
        if not db_objct:
            data = db_objct.attrs_to_dict()

            self.scrapely_parser.train_from_htmlpage(html, data)
            attr_dicts = self.scrapely_parser.scrape_page(html)

            for attr_dict in attr_dicts:
                objct = template._replicate(name=template.name, url=source.url)
                # Add the parsed values.
                objct.attrs_from_dict(attr_dict)
                yield objct
        return []

    def _convert_to_element(self, parsed):
        elements = []
        for p in parsed:
            if not type(p) == lxhtml.HtmlElement:
                elem = lxhtml.Element('p')
                elem.text = p
                elements.append(elem)
        return elements

    def sel_text(self, elements, replacers=None, substitute='', regex: str='',  # noqa
                numbers: bool=False, index=None, needle=None, all_text=True,
                split='', as_list=False, debug=False):  # noqa
        '''
        Select all text for a given selector.
        '''
        try:
            if all_text:
                text = [el.text_content() for el in elements]
            else:
                text = [el.text for el in elements]

            text = [t.lstrip().rstrip() for t in text if t]

            if replacers:
                text = [re.sub('{}'.format('|'.join(replacers)),
                            substitute, t) for t in text]
            if regex:
                text = [f for t in text for f in re.findall(regex, t)]
                # set types correctly
            if needle:
                if not all([re.match(needle, t) in t for te in text]):
                    return None

            if numbers:
                text = [int(''.join([c for c in t if c.isdigit() and c]))
                        for t in text if t and any(map(str.isdigit, t))]

            if text:
                if debug:
                    print(text)
                return self._value(text, index)
        except Exception as e:
            print(elements, e)


    def sel_table(self, elements, columns: int=2, offset: int=0):
        '''
        Parses a nxn table into a dictionary.
        Works best when the input is a td selector.
        Specify the amount of columns with the columns parameter.
        example:
            parse a 2x2 table
            {'func': sel_table,
            'params': {
                'selector': CSSSelector('table td'),
                'columns': 2,
                'offset': 0,
                }
            }
            leads to:
            sel_table(html=lxml.etree, selector=CSSSelector('table td'),
                    columns=2, offset=0)
        '''
        keys = [el.text for el in elements[offset::columns]]
        values = [el.text for el in elements[1::columns]]
        return dict(zip(keys, values))

    def sel_row(self, elements, row_selector: int=None, value: str='',
                attr=None, index=None):
        rows = [row for row in elements if value in row.text_contents()]
        if attr:
            selected = [sel for sel in sel_attr(row, row_selector)
                        for row in rows]
        else:
            selected = [sel for sel in sel_text(row, row_selector)
                        for row in rows]
        return self._value(selected, index)

    def sel_attr(self, elements, attr: str='', index: int=None,
                 regex=None):
        '''
        Extract an attribute of an HTML element. Will return
        a list of attributes if multiple tags match the
        selector.
        '''

        attrs = [el.attrib.get(attr) for el in elements]
        if regex:
            attrs = [f for a in attrs for f in re.findall(regex, a)]

        return self._value(attrs, index)

    def sel_url(self, elements, index: int=None, **kwargs):
        return self.sel_attr(elements, attr='href', index=index, **kwargs)

    def sel_date(self, elements, fmt: str='YYYYmmdd', attr: str=None, index: int=None):
        '''
        Returns a python date object with the specified format.
        '''
        if attr:
            date = sel_attr(html, selector, attr=attr, index=index)
        else:
            date = sel_text(html, selector, index=index)
        if date:
            return datetime.strptime(date, fmt)

    def sel_exists(self, elements, key: str='', index: int=None):
        '''
        Return True if a keyword is in the selector text,
        '''
        text = self.sel_text(elements)
        if text:
            if key in text:
                return True
            return False

    def sel_raw_html(self, elements):
        return [el.raw_html for el in elements]

    def sel_json(self, obj, selector, key=''):
        return obj.get(key)

    def sel_js_array(self, elements, var_name='', var_type=None):
        var_regex = 'var\s*'+var_name+'\s*=\s*(?:new Array\(|\[)(.*)(?:\)|\]);'
        array_string = self.sel_text(elements, regex=var_regex)
        if array_string:
            if var_type:
                return list(map(var_type, array_string.split(',')))
            return array_string.split(',')

    def fill_form(self, elements, fields={}, attrs=[]):
        for form in elements:
            data = {**dict(form.form_values()), **fields}
            source = Source(url=form.action, method=form.method, duplicate=True,
                            attrs=attrs)
            if source.method == 'GET':
                source.params = data
            else:
                source.data = data
            self._add_source(source)


class JSONParser(BaseParser):
    def __init__(self, data=None, selector=None):
        self.data = json.loads(data)
        if selector:
            for key in selector:
                self.data = self.data[key]

    def sel_key(self, selector, key=''):
        return self.data.get(key)
