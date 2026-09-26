"""Flask application exposing the slidedesk REST API and GUI."""
from __future__ import annotations

import io
import json
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, render_template, stream_with_context

from .. import convert, embeddings, text_generation
from ..project import SlideProject


def create_app(project: SlideProject, *, scan_enabled: bool = True) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(Path(__file__).parent / "static"),
        template_folder=str(Path(__file__).parent / "templates"),
    )
    app.config["JSON_SORT_KEYS"] = False
    generation_events = {}
    generation_events_lock = threading.Lock()

    def deck_file_exists(deck):
        return deck is not None and (project.folder / deck.pptx_path).is_file()

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

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/shutdown")
    def api_shutdown():
        print("Shutdown requested.")
        def _exit():
            try:
                print("Closing project...")
                project.close()
            finally:
                print("Exiting...")
                os._exit(0)

        threading.Timer(0.2, _exit).start()
        return jsonify({"ok": True})

    @app.get("/api/decks")
    def api_decks():
        decks = []
        for deck in project.decks():
            if not deck_file_exists(deck):
                continue
            slides = project.slides(deck.id)
            payload = deck_to_json(deck)
            payload["slides"] = [slide_to_json(s) for s in slides]
            decks.append(payload)
        return jsonify(decks)

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
        dpi = request.args.get("dpi", default=110, type=int)
        try:
            image = project.slide_image(slide_id, dpi=dpi)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 404
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png", max_age=3600)

    @app.get("/api/scan/status")
    def api_scan_status():
        return jsonify(project.scan_status())

    @app.post("/api/scan")
    def api_scan_trigger():
        if not scan_enabled:
            return jsonify({"error": "Folder scanning is disabled (--no-scan)."}), 403
        project.scan_in_background()
        return jsonify({"ok": True})

    @app.post("/api/decks/<int(signed=True):deck_id>/refresh")
    def api_deck_refresh(deck_id):
        if project.deck(deck_id) is None:
            return jsonify({"error": "deck not found"}), 404
        project.refresh_deck(deck_id)
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
