from multiprocessing import Process, JoinableQueue
from queue import Queue, Empty
import os

from pybloom import ScalableBloomFilter


class ScrapeWorker(Process):
    def __init__(self, model, store_q=JoinableQueue()):
        super(ScrapeWorker, self).__init__()
        '''
        self.name = name
        self.domain = domain
        self.runs = runs
        self.time_out = time_out
        self.num_getters = num_getters
        self.session = session
        self.daemon = daemon
        '''

        self.source_q = Queue()
        self.parse_q = JoinableQueue()
        self.store_q = store_q
        self.seen = ScalableBloomFilter()
        self.forwarded = ScalableBloomFilter()
        self.new_sources = []
        self.workers = []
        self.to_forward = []
        self.parser = None
        self.done_parsing = False
        self.no_more_sources = False

        for attr in model.__dict__.keys():
            setattr(self, attr, getattr(model, attr))

    def run(self):
        # create the threads needed to scrape
        i = 0
        while self.runs:
            run = self.runs.pop(0)
            print('running run:', i)
            i += 1

            # Check if the run has a parser, if not, reuse the one from the
            # last run.
            self.to_parse = 0
            self.parsed = 0

            if run.active:
                self.spawn_parser(run)
                self.spawn_workforce(run)
                self.add_sources(run)
                self.parse_sources()
            if run.repeat:
                self.runs.append(run)
                print('Repeating run', i-1)

            print('run', i-1, 'stopped')

    def parse_sources(self):
        while True:
            if self.to_parse == self.parsed:
                break
            try:
                source = self.parse_q.get(timeout=10)
            except Empty:
                if self.source_q.empty():
                    print('No more sources to parse at this point')
                    break
                else:
                    print('Waiting for sources to parse')
            self.seen.add(source.url)
            objects = self.parser.parse(source)
            self.parsed += 1

            for obj in objects:
                if obj.db:
                    self.store_q.put(obj)

            for new_source in self.new_sources:
                self._gen_source(*new_source)

            self.new_sources = []
            self.show_progress()

        # self.parser.join()
        print('parser_joined')
        print('Unparsed ', self.source_q.qsize())
        # print('forwarded', len(self.parser.forwarded))

    def spawn_parser(self, run):
        if run.parser:
            self.parser = run.parser(parent=self, templates=run.templates)
        elif not self.parser and not run.parser:
            raise Exception('No parser was specified')
        else:
            parse_class = self.parser.__class__
            self.parser = parse_class(parent=self, templates=run.templates)

    def spawn_workforce(self, run):
        # check if run reuses the current source workforce
        if run.n_workers:
            n_workers = run.n_workers
        else:
            n_workers = self.num_getters
        if not self.workers:
            for i in range(n_workers):
                worker = run.source_worker(parent=self, id=i,
                                           out_q=self.parse_q,
                                           time_out=self.time_out)
                worker.start()
                self.workers.append(worker)

    def add_sources(self, run):
        for source in self.to_forward:
            self.source_q.put(source)
            self.to_parse += 1

        self.to_forward = []

        for source in run.sources:
            if source.active:
                self.source_q.put(source)
                self.to_parse += 1

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

            if attr.source.copy_attrs:
                name = attr.source.copy_attrs
                new_source.attrs[name] = objct.attrs[name]()

            if attr.attr_condition and \
                    self.value_is_new(objct, value, attr.attr_condition):
                    self._add_source(new_source)
            else:
                self._add_source(new_source)

    def _add_source(self, source):
        if source.url and (source.url not in self.seen or source.duplicate) \
                and source.url not in self.forwarded:
            if source.active:
                self.to_parse += 1
                self.source_q.put(source)
                self.seen.add(source.url)
            else:
                self.to_forward.append(source)
                self.forwarded.add(source.url)

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

    def _evaluate_condition(self, objct, condition, **kwargs):
        # TODO add "in", and other possibilities.
        for name, cond in condition.items():
            values = objct.attrs[name].value
            # Wrap the value in a list without for example seperating the
            # characters.
            values = [values] if type(values) != list else values
            for val in values:
                if val and eval(str(val) + cond, {}, {}):
                    yield True
                else:
                    yield False

    def reset_source_queue(self):
        while not self.source_q.empty():
            try:
                self.source_q.get(False)
            except Empty:
                continue
            self.source_q.task_done()


    def show_progress(self):
        # os.system('clear')
        info = '''
        Domain            {}
        Sources to get:   {}
        Sources to parse: {}
        Sources parsed:   {}
        '''
        print(info.format(self.name,
                          self.source_q.qsize(),
                          self.to_parse,
                          self.parsed))
