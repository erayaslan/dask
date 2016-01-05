from timeit import default_timer
import sys

from dask.core import flatten
from toolz import valmap
from tornado import gen
from tornado.ioloop import IOLoop

from .progress import format_time, Progress, MultiProgress

from ..core import connect, read, write
from ..executor import default_executor
from ..utils import sync, ignoring, key_split


def get_scheduler(scheduler):
    if scheduler is None:
        scheduler = default_executor().scheduler
        return scheduler.ip, scheduler.port
    if isinstance(scheduler, str):
        scheduler = scheduler.split(':')
    if isinstance(scheduler, tuple):
        return scheduler
    raise TypeError("Expected None, string, or tuple")


class ProgressBar(object):
    def __init__(self, keys, scheduler=None, interval=0.1, complete=True):
        self.scheduler = get_scheduler(scheduler)

        self.keys = {k.key if hasattr(k, 'key') else k for k in keys}
        self.interval = interval
        self.complete = complete
        self._start_time = default_timer()

    @property
    def elapsed(self):
        return default_timer() - self._start_time

    @gen.coroutine
    def listen(self):
        complete = self.complete
        keys = self.keys

        def setup(scheduler):
            p = Progress(keys, scheduler, complete=complete)
            scheduler.add_plugin(p)
            return p

        def func(scheduler, p):
            return {'all': len(p.all_keys),
                    'remaining': len(p.keys),
                    'status': p.status}

        self.stream = yield connect(*self.scheduler)

        yield write(self.stream, {'op': 'feed',
                                  'setup': setup,
                                  'function': func,
                                  'interval': self.interval})

        while True:
            self._last_response = response = yield read(self.stream)
            self.status = response['status']
            self._draw_bar(**response)
            if response['status'] in ('error', 'finished'):
                self.stream.close()
                self._draw_stop(**response)
                break

    def _draw_stop(self, **kwargs):
        pass


class TextProgressBar(ProgressBar):
    def __init__(self, keys, scheduler=None, interval=0.1, width=40,
                       loop=None, complete=True, start=True):
        super(TextProgressBar, self).__init__(keys, scheduler, interval,
                                              complete)
        self.width = width
        self.loop = loop or IOLoop()

        if start:
            sync(self.loop, self.listen)

    def _draw_bar(self, remaining, all, **kwargs):
        frac = (1 - remaining / all) if all else 1.0
        bar = '#' * int(self.width * frac)
        percent = int(100 * frac)
        elapsed = format_time(self.elapsed)
        msg = '\r[{0:<{1}}] | {2}% Completed | {3}'.format(bar, self.width,
                                                           percent, elapsed)
        with ignoring(ValueError):
            sys.stdout.write(msg)
            sys.stdout.flush()


class ProgressWidget(ProgressBar):
    """ ProgressBar that uses an IPython ProgressBar widget for the notebook

    See Also
    --------
    progress: User function
    TextProgressBar: Text version suitable for the console
    """
    def __init__(self, keys, scheduler=None, interval=0.1,
                complete=False, loop=None):
        super(ProgressWidget, self).__init__(keys, scheduler, interval,
                                             complete)

        from ipywidgets import FloatProgress, HBox, VBox, HTML
        self.elapsed_time = HTML('')
        self.bar = FloatProgress(min=0, max=1, description='', height = '10px')
        self.bar_text = HTML('', width = "140px")

        self.bar_widget = HBox([ self.bar_text, self.bar ])
        self.widget = VBox([self.elapsed_time, self.bar_widget])

    def _ipython_display_(self, **kwargs):
        IOLoop.current().add_callback(self.listen)
        return self.widget._ipython_display_(**kwargs)

    def _draw_stop(self, remaining, status, **kwargs):
        if status == 'error':
            self.bar.bar_style = 'danger'
            self.elapsed_time.value = '<div style="padding: 0px 10px 5px 10px"><b>Warning:</b> the computation terminated due to an error after ' + format_time(self.elapsed) + '</div>'
        elif not remaining:
            self.bar.bar_style = 'success'

    def _draw_bar(self, remaining, all, **kwargs):
        ndone = all - remaining
        self.elapsed_time.value = '<div style=\"padding: 0px 10px 5px 10px\"><b>Elapsed time:</b> ' + format_time(self.elapsed) + '</div>'
        self.bar.value = ndone / all if all else 1.0
        self.bar_text.value = '<div style="padding: 0px 10px 0px 10px; text-align:right;">%d / %d</div>' % (ndone, all)



class MultiProgressBar(object):
    def __init__(self, keys, scheduler=None, func=key_split, interval=0.1, complete=False):
        self.scheduler = get_scheduler(scheduler)

        self.keys = {k.key if hasattr(k, 'key') else k for k in keys}
        self.func = func
        self.interval = interval
        self.complete = complete
        self._start_time = default_timer()

    @property
    def elapsed(self):
        return default_timer() - self._start_time

    @gen.coroutine
    def listen(self):
        complete = self.complete
        keys = self.keys
        func = self.func

        def setup(scheduler):
            p = MultiProgress(keys, scheduler, complete=complete, func=func)
            scheduler.add_plugin(p)
            return p

        def function(scheduler, p):
            return {'all': valmap(len, p.all_keys),
                    'remaining': valmap(len, p.keys),
                    'status': p.status}

        self.stream = yield connect(*self.scheduler)

        yield write(self.stream, {'op': 'feed',
                                  'setup': setup,
                                  'function': function,
                                  'interval': self.interval})

        while True:
            self._last_response = response = yield read(self.stream)
            self.status = response['status']
            self._draw_bar(**response)
            if response['status'] in ('error', 'finished'):
                self.stream.close()
                self._draw_stop(**response)
                break

    def _draw_stop(self, **kwargs):
        pass


class MultiProgressWidget(MultiProgressBar):
    """ Multiple progress bar Widget suitable for the notebook

    Displays multiple progress bars for a computation, split on computation
    type.

    See Also
    --------
    progress: User-level function <--- use this
    MultiProgress: Non-visualization component that contains most logic
    ProgressWidget: Single progress bar widget
    """
    def __init__(self, keys, scheduler=None, minimum=0, interval=0.1, func=key_split,
                 complete=False):
        super(MultiProgressWidget, self).__init__(keys, scheduler, func, interval, complete)

        # Set up widgets

    def make_widget(self, all):
        from ipywidgets import FloatProgress, HBox, VBox, HTML
        import cgi
        self.elapsed_time = HTML('')
        self.bars = {key: FloatProgress(min=0, max=1, description='', height = '10px')
                        for key in all}
        self.bar_texts = {key: HTML('', width = "140px") for key in all}
        self.bar_labels = {key: HTML('<div style=\"padding: 0px 10px 0px 10px; text-align:left; word-wrap: break-word;\">'
                                     + cgi.escape(key) + '</div>') for key in all}

        # Check to see if 'finalize' is one of the keys. If it is, move it to
        # the end so that it is rendered last in the list (for aesthetics...)
        key_order = set(all.keys())
        if 'finalize' in key_order:
            key_order.remove('finalize')
            key_order = sorted(list(key_order)) + ['finalize']
        else:
            key_order = list(key_order)

        self.bar_widgets = VBox([ HBox([ self.bar_texts[key], self.bars[key], self.bar_labels[key] ]) for key in key_order ])
        self.widget = VBox([self.elapsed_time, self.bar_widgets])

    def _ipython_display_(self, **kwargs):
        return self.widget._ipython_display_(**kwargs)

    def _draw_stop(self, remaining, status, exception=None, key=None, **kwargs):
        for k, v in remaining.items():
            if not v:
                self.bars[k].bar_style = 'success'

        """ TODO
        if status == 'error':
            self.bars[self.func(key)].bar_style = 'danger'
            self.elapsed_time.value = '<div style="padding: 0px 10px 5px 10px"><b>Warning:</b> the computation terminated due to an error after ' + format_time(self.elapsed) + '</div>'
        """

    def _draw_bar(self, remaining, all, status, **kwargs):
        if not hasattr(self, 'widget'):
            self.make_widget(all)
        for k, ntasks in all.items():
            ndone = ntasks - remaining[k]
            self.elapsed_time.value = '<div style="padding: 0px 10px 5px 10px"><b>Elapsed time:</b> ' + format_time(self.elapsed) + '</div>'
            self.bars[k].value = ndone / ntasks if ntasks else 1.0
            self.bar_texts[k].value = '<div style="padding: 0px 10px 0px 10px; text-align: right">%d / %d</div>' % (ndone, ntasks)


def progress(*futures, **kwargs):
    """ Track progress of futures

    This operates differently in the notebook and the console

    *  Notebook:  This returns immediately, leaving an IPython widget on screen
    *  Console:  This blocks until the computation completes

    Parameters
    ----------
    futures: Futures
        A list of futures or keys to track
    notebook: bool (optional)
        Running in the notebook or not (defaults to guess)
    multi: bool (optional)
        Track different functions independently (defaults to True)
    complete: bool (optional)
        Track all keys (True) or only keys that have not yet run (False)
        (defaults to True)

    Examples
    --------
    >>> progress(futures)  # doctest: +SKIP
    [########################################] | 100% Completed |  1.7s
    """
    notebook = kwargs.pop('notebook', None)
    multi = kwargs.pop('multi', True)
    complete = kwargs.pop('complete', True)
    assert not kwargs

    futures = list(flatten(list(futures)))
    if not isinstance(futures, (set, list)):
        futures = [futures]
    if notebook is None:
        notebook = is_kernel()  # often but not always correct assumption
    if notebook:
        if multi:
            bar = MultiProgressWidget(futures, complete=complete)
        else:
            bar = ProgressWidget(futures, complete=complete)
        return bar
    else:
        TextProgressBar(futures, complete=complete)
