"""Flask application exposing the slidedesk REST API and GUI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, render_template, stream_with_context

from .. import convert, embeddings, images, text_generation
from ..project import SlideProject


def create_app(project: SlideProject, *, scan_enabled: bool = True, on_shutdown=None) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(Path(__file__).parent / "static"),
        template_folder=str(Path(__file__).parent / "templates"),
    )
    app.config["JSON_SORT_KEYS"] = False
    generation_events = {}
    generation_events_lock = threading.Lock()
    # Recent /api/decks versions -> {deck_id: revision}, for incremental updates.
    deck_snapshots = OrderedDict()
    deck_snapshots_lock = threading.Lock()

    def deck_file_exists(deck):
        return deck is not None and (project.folder / deck.pptx_path).is_file()

    def deck_file_path(deck):
        if deck is None:
            return None
        project_folder = project.folder.resolve()
        deck_path = (project_folder / deck.pptx_path).resolve()
        if not deck_path.is_relative_to(project_folder) or not deck_path.is_file():
            return None
        return deck_path

    def deck_to_json(deck):
        return {
            "id": deck.id,
            "name": deck.name,
            "pptx_path": deck.pptx_path,
            "pptx_mtime": deck.pptx_mtime,
            "pdf_path": deck.pdf_path,
            "pdf_mtime": deck.pdf_mtime,
            "hidden_pdf_path": deck.hidden_pdf_path,
            "hidden_pdf_mtime": deck.hidden_pdf_mtime,
            "last_scanned": deck.last_scanned,
        }

    def slide_to_json(slide):
        return {
            "id": slide.id,
            "deck_id": slide.deck_id,
            "index_in_deck": slide.index_in_deck,
            "visible_pdf_page": slide.visible_pdf_page,
            "hidden": slide.hidden,
            "text": slide.text,
        }

    # Trace API actions: print when each request starts and finishes. The
    # browser sends its send time in X-Client-Time (ms since epoch), so the
    # delay between the click-triggered fetch and server start is visible too.
    # Frequent polling and image requests are skipped to keep the output usable.
    untraced_endpoints = {"static", "api_scan_status", "api_embeddings_status", "api_slide_image"}

    # Skipped endpoints are still reported when they answer with anything other
    # than a plain 200, explaining what the status code means here.
    status_explanations = {
        202: "accepted, work queued in background; browser retries",
        304: "not modified, browser cache is still valid; nothing sent",
        404: "not found",
        500: "server error",
    }

    def trace_label():
        return f"{request.method} {request.full_path.rstrip('?')} [{request.endpoint}]"

    @app.before_request
    def trace_request_start():
        request.environ["slidedesk.trace_start"] = time.time()
        if request.endpoint in untraced_endpoints:
            return
        delay = ""
        client_time = request.headers.get("X-Client-Time")
        if client_time:
            try:
                delay = f" ({time.time() * 1000 - float(client_time):.0f} ms after browser sent it)"
            except ValueError:
                pass
        print(f"[{time.strftime('%H:%M:%S')}] START {trace_label()}{delay}", flush=True)

    @app.after_request
    def trace_request_end(response):
        started = request.environ.get("slidedesk.trace_start")
        if started is None:
            return response
        label = trace_label()
        status = response.status_code
        if request.endpoint in untraced_endpoints:
            if request.endpoint == "static" or status == 200:
                return response
            explanation = status_explanations.get(status, "")
            if status >= 400 and response.is_json:
                explanation = f"{explanation}: {(response.get_json(silent=True) or {}).get('error', '')}"
            elapsed = (time.time() - started) * 1000
            print(
                f"[{time.strftime('%H:%M:%S')}] {label} -> {status} in {elapsed:.0f} ms ({explanation})",
                flush=True,
            )
            return response

        def report():
            elapsed = (time.time() - started) * 1000
            print(f"[{time.strftime('%H:%M:%S')}] DONE  {label} -> {status} in {elapsed:.0f} ms", flush=True)

        response.call_on_close(report)
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/shutdown")
    def api_shutdown():
        print("Shutdown requested.")
        def _exit():
            try:
                if on_shutdown is not None:
                    on_shutdown()
                print("Closing project...")
                project.close()
            except Exception as exc:
                print(f"Error during shutdown: {exc}")
            finally:
                print("Exiting...")
                os._exit(0)

        threading.Timer(0.2, _exit).start()
        return jsonify({"ok": True})

    def decks_payload(decks, deck_ids=None):
        slides_by_deck = project.slides_by_deck(deck_ids)
        payload = []
        for deck in decks:
            entry = deck_to_json(deck)
            entry["slides"] = [slide_to_json(s) for s in slides_by_deck.get(deck.id, [])]
            payload.append(entry)
        return payload

    @app.get("/api/decks")
    def api_decks():
        """List decks with their slides.

        With `?since=<version>` (from a previous response), only decks changed
        since that version are sent, plus the ids of removed decks. An empty or
        unknown version yields the full list with `"full": true`.
        """
        decks = [deck for deck in project.decks() if deck_file_exists(deck)]
        if "since" not in request.args:
            return jsonify(decks_payload(decks))
        revisions = project.deck_revisions()
        current = {deck.id: revisions.get(deck.id, "") for deck in decks}
        version = hashlib.sha1(repr(sorted(current.items())).encode()).hexdigest()[:16]
        with deck_snapshots_lock:
            deck_snapshots[version] = current
            deck_snapshots.move_to_end(version)
            while len(deck_snapshots) > 16:
                deck_snapshots.popitem(last=False)
            previous = deck_snapshots.get(request.args["since"])
        if previous is None:
            return jsonify({"version": version, "full": True,
                            "decks": decks_payload(decks), "removed": []})
        changed = [deck for deck in decks if previous.get(deck.id) != current[deck.id]]
        removed = [deck_id for deck_id in previous if deck_id not in current]
        return jsonify({
            "version": version, "full": False,
            "decks": decks_payload(changed, [deck.id for deck in changed]) if changed else [],
            "removed": removed,
        })

    @app.get("/api/decks/<int(signed=True):deck_id>/slides")
    def api_deck_slides(deck_id):
        if not deck_file_exists(project.deck(deck_id)):
            return jsonify([])
        include_hidden = request.args.get("hidden", "true").lower() != "false"
        slides = project.slides(deck_id, include_hidden=include_hidden)
        return jsonify([slide_to_json(s) for s in slides])

    @app.patch("/api/slides/<int(signed=True):slide_id>/hidden")
    def api_slide_hidden(slide_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get("hidden")) is not bool:
            return jsonify({"error": "hidden must be a boolean"}), 400
        try:
            project.set_slide_hidden(slide_id, body["hidden"])
        except KeyError:
            return jsonify({"error": "slide not found"}), 404
        return jsonify({"ok": True})

    @app.post("/api/slides/<int(signed=True):slide_id>/copy")
    def api_slide_copy(slide_id):
        try:
            project.copy_slide_to_clipboard(slide_id)
        except KeyError:
            return jsonify({"error": "slide not found"}), 404
        except convert.ConversionError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})

    @app.patch("/api/decks/<int(signed=True):deck_id>/slides/hidden")
    def api_deck_slides_hidden(deck_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get("hidden")) is not bool:
            return jsonify({"error": "hidden must be a boolean"}), 400
        try:
            project.set_deck_slides_hidden(deck_id, body["hidden"])
        except KeyError:
            return jsonify({"error": "deck not found"}), 404
        return jsonify({"ok": True})

    @app.get("/api/search")
    def api_search():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])
        try:
            slides = project.search(query)
        except Exception:
            # FTS5 query syntax error, e.g. from a lone quote -> quote it literally.
            slides = project.search(f'"{query}"')
        by_deck = {}
        for slide in slides:
            by_deck.setdefault(slide.deck_id, []).append(slide_to_json(slide))
        results = []
        for deck_id, matched_slides in by_deck.items():
            deck = project.deck(deck_id)
            if not deck_file_exists(deck):
                continue
            payload = deck_to_json(deck)
            payload["matched_slides"] = matched_slides
            payload["slides"] = [slide_to_json(s) for s in project.slides(deck_id)]
            results.append(payload)
        return jsonify(results)

    @app.get("/api/search/semantic")
    def api_search_semantic():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])
        try:
            slides = project.search_semantic(query)
        except embeddings.EmbeddingError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502
        rank = {slide.id: i for i, slide in enumerate(slides)}
        by_deck = {}
        for slide in slides:
            by_deck.setdefault(slide.deck_id, []).append(slide_to_json(slide))
        results = []
        for deck_id, matched_slides in by_deck.items():
            deck = project.deck(deck_id)
            if not deck_file_exists(deck):
                continue
            payload = deck_to_json(deck)
            payload["matched_slides"] = matched_slides
            payload["slides"] = [slide_to_json(s) for s in project.slides(deck_id)]
            results.append(payload)
        # keep decks ordered by their best-matching (highest-scored) slide
        results.sort(key=lambda d: min(rank[s["id"]] for s in d["matched_slides"]))
        return jsonify(results)

    @app.post("/api/search/advanced/generate-list")
    def api_advanced_search_generate_list():
        body = request.get_json(silent=True)
        prompt = body.get("prompt") if isinstance(body, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"error": "prompt must be a non-empty string"}), 400
        try:
            return jsonify({"list": text_generation.generate_list(prompt.strip())})
        except text_generation.TextGenerationError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.post("/api/search/advanced/generate-list/stream")
    def api_advanced_search_generate_list_stream():
        body = request.get_json(silent=True)
        prompt = body.get("prompt") if isinstance(body, dict) else None
        generation_id = body.get("generation_id") if isinstance(body, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"error": "prompt must be a non-empty string"}), 400
        if not isinstance(generation_id, str) or not generation_id:
            return jsonify({"error": "generation_id must be a non-empty string"}), 400
        cancel_event = threading.Event()
        with generation_events_lock:
            generation_events[generation_id] = cancel_event

        @stream_with_context
        def generate_events():
            try:
                for chunk in text_generation.generate_list_stream(prompt.strip(), cancel_event):
                    if cancel_event.is_set():
                        break
                    yield json.dumps({"text": chunk}) + "\n"
                yield json.dumps({"cancelled": cancel_event.is_set(), "done": True}) + "\n"
            except text_generation.TextGenerationError as exc:
                yield json.dumps({"error": str(exc), "done": True}) + "\n"
            finally:
                cancel_event.set()
                with generation_events_lock:
                    generation_events.pop(generation_id, None)

        return Response(generate_events(), mimetype="application/x-ndjson")

    @app.post("/api/search/advanced/generate-list/cancel")
    def api_advanced_search_cancel_generation():
        body = request.get_json(silent=True)
        generation_id = body.get("generation_id") if isinstance(body, dict) else None
        if not isinstance(generation_id, str) or not generation_id:
            return jsonify({"error": "generation_id must be a non-empty string"}), 400
        with generation_events_lock:
            cancel_event = generation_events.get(generation_id)
        if cancel_event is not None:
            cancel_event.set()
        return jsonify({"ok": True, "cancelled": cancel_event is not None})

    @app.post("/api/search/advanced/semantic")
    def api_advanced_search_semantic():
        body = request.get_json(silent=True)
        query = body.get("query") if isinstance(body, dict) else None
        top_k = body.get("top_k", 2) if isinstance(body, dict) else 2
        if not isinstance(query, str) or not query.strip():
            return jsonify({"error": "query must be a non-empty string"}), 400
        if type(top_k) is not int or not 1 <= top_k <= 50:
            return jsonify({"error": "top_k must be an integer between 1 and 50"}), 400
        try:
            slides = project.search_semantic(query.strip(), top_k=top_k)
        except embeddings.EmbeddingError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify([
            slide_to_json(slide) for slide in slides
            if deck_file_exists(project.deck(slide.deck_id))
        ])

    @app.get("/api/slides/<int(signed=True):slide_id>/similar")
    def api_slide_similar(slide_id):
        source = project.slide(slide_id)
        if source is None or not deck_file_exists(project.deck(source.deck_id)):
            return jsonify({"error": "slide not found"}), 404
        try:
            slides = project.similar_slides(slide_id)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502
        available_decks = {
            deck_id: deck_file_exists(project.deck(deck_id))
            for deck_id in {s.deck_id for s in slides}
        }
        return jsonify([slide_to_json(s) for s in slides if available_decks[s.deck_id]])

    @app.get("/api/embeddings/status")
    def api_embeddings_status():
        return jsonify(project.embedding_status())

    @app.get("/api/slides/<int(signed=True):slide_id>/image")
    def api_slide_image(slide_id):
        layout = request.args.get("layout", "true").lower() != "false"
        try:
            path = project.slide_image_path(slide_id, layout=layout)
        except (KeyError, FileNotFoundError) as exc:
            return jsonify({"error": str(exc)}), 404
        except images.RenderError as exc:
            return jsonify({"error": str(exc)}), 500
        if path is None:
            # Rendering is queued in the background; the browser retries shortly.
            response = Response(status=202)
            response.headers["Retry-After"] = "1"
            response.headers["Cache-Control"] = "no-store"
            return response
        return send_file(path, mimetype="image/png", max_age=3600)

    @app.get("/api/scan/status")
    def api_scan_status():
        return jsonify(project.scan_status())

    @app.post("/api/scan")
    def api_scan_trigger():
        if not scan_enabled:
            return jsonify({"error": "Folder scanning is disabled (--no-scan)."}), 403
        # One-shot refresh; continuous scanning runs in the --scan-only process.
        threading.Thread(target=project.scan, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/decks/<int(signed=True):deck_id>/refresh")
    def api_deck_refresh(deck_id):
        if project.deck(deck_id) is None:
            return jsonify({"error": "deck not found"}), 404
        project.refresh_deck(deck_id)
        return jsonify({"ok": True})

    @app.post("/api/decks/<int(signed=True):deck_id>/open")
    def api_deck_open(deck_id):
        deck_path = deck_file_path(project.deck(deck_id))
        if deck_path is None:
            return jsonify({"error": "slide deck not found"}), 404
        if not hasattr(os, "startfile"):
            return jsonify({"error": "Opening slide decks is only supported on Windows"}), 501
        try:
            os.startfile(str(deck_path))
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/decks/<int(signed=True):deck_id>/open-folder")
    def api_deck_open_folder(deck_id):
        deck_path = deck_file_path(project.deck(deck_id))
        if deck_path is None:
            return jsonify({"error": "slide deck not found"}), 404
        try:
            subprocess.Popen(["explorer", f"/select,{deck_path}"])
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True})

    @app.post("/api/export")
    def api_export():
        body = request.get_json(force=True, silent=True) or {}
        slide_ids = body.get("slide_ids") or []
        try:
            out_path = project.export_selection([int(i) for i in slide_ids])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except convert.ConversionError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True, "path": out_path.name})

    @app.post("/api/copy")
    def api_copy():
        body = request.get_json(force=True, silent=True) or {}
        slide_ids = body.get("slide_ids") or []
        try:
            project.copy_selection_to_clipboard([int(i) for i in slide_ids])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except convert.ConversionError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({"ok": True})

    @app.get("/api/export/<path:filename>/download")
    def api_export_download(filename):
        filename = Path(filename).name
        out_path = project.export_dir / filename
        if not out_path.exists():
            return jsonify({"error": "not found"}), 404
        return send_file(out_path, as_attachment=True, download_name=filename)

    return app
