"""The Models view: search Hugging Face for GGUF models as you type, see one model's facts and its model card, and
download a quantization in the background (mini.models). The preset maker (/preset-new) is separate: it uses
what is in the models folder.

  [ search box                                              ]
  [ results          ] [ facts, quantizations, model card    ]
  [ quantization v ] [x] vision   [ Download ]   message
"""
from rich.markup import escape
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Markdown, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import models

SEARCH_DELAY_S = 0.4          # typing pauses this long before the search runs again
PREVIEW_DELAY_S = 0.25        # moving through the list pauses this long before a model is read
FITS = 0.85                   # a file up to this share of the GPUs' VRAM leaves room for the context


class ModelBrowser(Vertical):
    DEFAULT_CSS = """
    ModelBrowser { height: 1fr; }
    #mb-search { margin: 0 0 1 0; }
    #mb-main { height: 1fr; }
    #mb-results { width: 2fr; height: 1fr; }
    #mb-preview { width: 3fr; height: 1fr; border: round $primary-darken-2; padding: 0 1; }
    #mb-info { height: auto; margin: 0 0 1 0; }
    #mb-bar { height: auto; margin: 1 0 0 0; }
    #mb-quant { width: 50; }
    #mb-vision { width: auto; }
    #mb-msg { width: 1fr; padding: 1 1 0 1; }
    """

    def __init__(self, app_ref, **kw):
        super().__init__(**kw)
        self.mini = app_ref                     # the MiniApp: its cfg, chat and poll_downloads
        self.hits, self.cache = [], {}          # search results; repo -> (info, options, projectors, card)
        self.repo = None                        # the model shown in the preview
        self.search_gen = self.preview_gen = 0  # results of an older search or preview are dropped
        self._search_timer = self._preview_timer = None
        self.vram = None                        # bytes of VRAM of the discrete GPUs, once known
        self.searched = False

    def compose(self):
        yield Input(placeholder="Search Hugging Face for GGUF models (empty: the most downloaded)", id="mb-search")
        with Horizontal(id="mb-main"):
            yield OptionList(id="mb-results")
            with VerticalScroll(id="mb-preview"):
                yield Static("Pick a model on the left.", id="mb-info")
                yield Markdown("", id="mb-card")
        with Horizontal(id="mb-bar"):
            yield Select([], prompt="quantization", id="mb-quant")
            yield Checkbox("vision projector", id="mb-vision", disabled=True)
            yield Button("Download", id="mb-download", variant="success", disabled=True)
            yield Static("", id="mb-msg")

    # ---------------------------------------------------------------- opening
    def opened(self, query=None):
        """The view was shown: focus the search box, and search once (or for query)."""
        box = self.query_one("#mb-search", Input)
        if query is not None and query != box.value:
            box.value = query                  # Changed runs the search
        elif not self.searched:
            self.search(box.value)
        box.focus()
        if self.vram is None:
            self.run_worker(self._read_vram, thread=True, group="mb-vram")

    def _read_vram(self):
        from .. import setup
        try:
            _build, server = setup.listing_server(self.mini.cfg)
            cards = [c for c in setup.gpu_cards(setup.gpu_devices(server)) if not c["integrated"]]
            vram = sum(c["mib"] for c in cards) * 1048576
        except Exception:   # noqa: BLE001 - without llama.cpp there is no VRAM figure; sizes are still shown
            vram = 0
        self.app.call_from_thread(self.vram_known, vram)

    def vram_known(self, vram):
        """The quantizations already shown get their fits / too big mark."""
        self.vram = vram
        if vram and getattr(self, "options", None):
            sel = self.query_one("#mb-quant", Select)
            keep = sel.value
            self.set_quants(self.options)
            if keep is not Select.BLANK:
                sel.value = keep

    # ---------------------------------------------------------------- search as you type
    def on_input_changed(self, ev):
        if ev.input.id != "mb-search":
            return
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(SEARCH_DELAY_S, lambda: self.search(ev.value))

    def on_input_submitted(self, ev):
        if ev.input.id == "mb-search":
            if self._search_timer:
                self._search_timer.stop()
            self.search(ev.value)
            self.query_one("#mb-results", OptionList).focus()

    def search(self, query):
        self.searched = True
        self.search_gen += 1
        gen = self.search_gen
        self.message(f"searching for {escape(repr(query))} ..." if query.strip() else "the most downloaded GGUF models ...")

        def work():
            try:
                hits, err = models.browse(query), ""
            except models.DownloadError as e:
                hits, err = [], str(e)
            self.app.call_from_thread(self.show_hits, gen, query, hits, err)
        self.run_worker(work, thread=True, group="mb-search")

    def show_hits(self, gen, query, hits, err):
        if gen != self.search_gen:
            return                              # a newer search is on its way
        self.hits = hits
        ol = self.query_one("#mb-results", OptionList)
        ol.clear_options()
        ol.add_options([Option(self.row(h), id=h["id"]) for h in hits])
        q = escape(repr(query))
        self.message(escape(err) if err else (f"{len(hits)} models" + (f" for {q}" if query.strip() else ", most downloaded first")
                                              if hits else f"nothing found for {q}"))
        if hits:
            ol.highlighted = 0

    @staticmethod
    def row(h):
        facts = [models.short_number(h["params"]) + " params" if h["params"] else "", h["arch"],
                 f"{h['context'] // 1024}k ctx" if h["context"] else "", "vision" if h["vision"] else "",
                 f"↓{models.short_number(h['downloads'])}", f"♥{models.short_number(h['likes'])}"]
        t = Text(h["id"], style="bold")
        t.append("\n" + " · ".join(f for f in facts if f), style="dim")
        return t

    # ---------------------------------------------------------------- one model
    def on_option_list_option_highlighted(self, ev):
        if ev.option_list.id != "mb-results" or ev.option is None:
            return
        if self._preview_timer:
            self._preview_timer.stop()
        repo = ev.option.id
        self._preview_timer = self.set_timer(PREVIEW_DELAY_S, lambda: self.preview(repo))

    def preview(self, repo):
        self.repo = repo
        self.preview_gen += 1
        gen = self.preview_gen
        if repo in self.cache:
            self.show_preview(gen, repo, *self.cache[repo])
            return
        self.query_one("#mb-info", Static).update(f"[b]{escape(repo)}[/b]\n[dim]reading ...[/dim]")
        self.query_one("#mb-download", Button).disabled = True

        def work():
            try:
                about = models.info(repo)
                options, proj = models.choices(models.tree(repo))
                card = models.readme(repo)
                out = (about, options, proj, card, "")
                self.cache[repo] = out[:4] + ("",)
            except models.DownloadError as e:
                out = ({"id": repo}, [], [], "", str(e))
            self.app.call_from_thread(self.show_preview, gen, repo, *out)
        self.run_worker(work, thread=True, group="mb-preview")

    def show_preview(self, gen, repo, about, options, proj, card, err=""):
        if gen != self.preview_gen:
            return
        self.options, self.proj = options, proj
        info = self.query_one("#mb-info", Static)
        if err:
            info.update(f"[b]{escape(repo)}[/b]\n[red]{escape(err)}[/red]")
            self.set_quants([])
            self.query_one("#mb-card", Markdown).update("")
            return
        facts = [("parameters", models.short_number(about["params"]) if about.get("params") else "-"),
                 ("architecture", about.get("arch") or "-"),
                 ("context", f"{about['context']:,} tokens" if about.get("context") else "-"),
                 ("vision", "yes" + (" (projector available)" if proj else "") if about.get("vision") or proj else "no"),
                 ("license", about.get("license") or "-"), ("based on", about.get("base") or "-"),
                 ("downloads", f"{about.get('downloads', 0):,}   likes {about.get('likes', 0):,}"),
                 ("updated", about.get("updated") or "-")]
        if self.vram:
            facts.append(("your GPUs", f"{models.human(self.vram)} VRAM (discrete cards)"))
        info.update(f"[b]{escape(repo)}[/b]\n" + "\n".join(f"[dim]{k:>13}[/dim]  {escape(str(v))}" for k, v in facts)
                    + ("\n[yellow]gated: needs a login, mini cannot download it[/yellow]" if about.get("gated") else ""))
        self.set_quants(options)
        vision = self.query_one("#mb-vision", Checkbox)
        vision.disabled, vision.value = not proj, bool(proj) and vision.value
        self.query_one("#mb-card", Markdown).update(card or "_(no model card)_")
        self.query_one("#mb-preview", VerticalScroll).scroll_home(animate=False)

    def quant_label(self, o):
        label = f"{o['quant'] or o['name']}  {models.human(o['size'])}"
        if len(o["files"]) > 1:
            label += f"  ({len(o['files'])} parts)"
        if self.vram:
            label += "  fits" if o["size"] <= self.vram * FITS else "  larger than your VRAM"
        return label

    def default_quant(self, options):
        fits = [o for o in options if not self.vram or o["size"] <= self.vram * FITS]
        for o in fits:
            if o["quant"].upper().endswith("Q4_K_M"):
                return o
        return (fits or options)[-1 if fits else 0]

    def set_quants(self, options):
        sel = self.query_one("#mb-quant", Select)
        sel.set_options([(self.quant_label(o), o["name"]) for o in options])
        if options:
            sel.value = self.default_quant(options)["name"]
        self.query_one("#mb-download", Button).disabled = not options

    # ---------------------------------------------------------------- download
    def on_button_pressed(self, ev):
        if ev.button.id != "mb-download" or not self.repo:
            return
        ev.stop()
        quant = self.query_one("#mb-quant", Select).value
        if quant is Select.BLANK:
            self.message("pick a quantization first")
            return
        repo, vision = self.repo, self.query_one("#mb-vision", Checkbox).value
        ev.button.disabled = True
        self.message(f"starting {quant} ...")

        def work():
            try:
                job = models.download(self.mini.cfg, repo, quant, None, vision)
                msg = (f"[green]downloading {escape(job['name'])}[/green] ({models.human(job['size'])}) in the background; "
                       "it keeps going when mini closes")
            except models.DownloadError as e:
                msg = f"[yellow]{escape(str(e))}[/yellow]"
            self.app.call_from_thread(self.downloaded, msg)
        self.run_worker(work, thread=True, group="mb-download")

    def downloaded(self, msg):
        self.message(msg)
        self.query_one("#mb-download", Button).disabled = False
        self.mini.chat(msg)
        self.mini.poll_downloads()

    def message(self, text):
        self.query_one("#mb-msg", Static).update(text)
