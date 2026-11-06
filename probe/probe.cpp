// SPDX-License-Identifier: GPL-2.0-or-later
/*
	qetdemoprobe: loaded into an unmodified QElectroTech with LD_PRELOAD to
	tell a how-to-video recorder where things are on screen and what the
	program just did. Public Qt API only. Does nothing unless
	QET_DEMO_PROBE_SOCKET names a socket to listen on.

	Protocol: one JSON object per line each way. Requests carry "cmd" and an
	optional "id", which the answer repeats. A connection that sends
	{"cmd":"subscribe"} also receives {"event":...} lines.
	All boxes are [x, y, w, h] in physical screen pixels.
*/
#include <QAbstractButton>
#include <QAction>
#include <QApplication>
#include <QDialog>
#include <QLabel>
#include <QAbstractItemView>
#include <QCheckBox>
#include <QComboBox>
#include <QCursor>
#include <QToolTip>
#include <QMessageBox>
#include <QGraphicsItem>
#include <QGraphicsPathItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalServer>
#include <QLocalSocket>
#include <QMainWindow>
#include <QMenu>
#include <QMenuBar>
#include <QScrollBar>
#include <QListWidget>
#include <QStatusBar>
#include <QTabBar>
#include <QTimer>
#include <QToolBar>
#include <QUndoStack>
#include <QWindow>

namespace {

QString plain(QString s)
{
	s.remove(QLatin1Char('&'));
	s.remove(QStringLiteral("…"));
	return s.trimmed();
}

QJsonArray box(const QWidget *w, const QRect &local)
{
	const qreal r = w->devicePixelRatioF();
	const QPoint g = w->mapToGlobal(local.topLeft());
	return {qRound(g.x() * r), qRound(g.y() * r), qRound(local.width() * r), qRound(local.height() * r)};
}

QJsonArray boxF(const QWidget *w, const QRectF &local)
{
	return box(w, local.toAlignedRect());
}

class Probe : public QObject
{
public:
	explicit Probe(const QString &path) : QObject(qApp)
	{
		QLocalServer::removeServer(path);
		m_server = new QLocalServer(this);
		m_server->setSocketOptions(QLocalServer::UserAccessOption);
		m_server->listen(path);
		connect(m_server, &QLocalServer::newConnection, this, &Probe::accept);
		qApp->installEventFilter(this);
		auto *t = new QTimer(this);
		connect(t, &QTimer::timeout, this, &Probe::scan);
		t->start(300);
	}

	bool eventFilter(QObject *o, QEvent *e) override
	{
		// Tooltips of the drawing (a handle's "Click: mode Rounded corners")
		// only clutter a recording; those of toolbars and menus stay.
		static const bool keepTips = qEnvironmentVariableIsSet("QET_DEMO_PROBE_KEEP_TOOLTIPS");
		if (!keepTips && e->type() == QEvent::ToolTip) {
			if (auto *w = qobject_cast<QWidget *>(o))
				if (qobject_cast<QGraphicsView *>(w->parentWidget()))
					return true;
		}
		// ...including those QET shows itself (QToolTip::showText, no
		// ToolTip event): hide the tooltip window if the pointer is on a sheet.
		if (!keepTips && e->type() == QEvent::Show && o->inherits("QTipLabel")) {
			// any ancestor: right after a drag-select the rubber band, a
			// child of the sheet's viewport, is the widget under the pointer
			for (QWidget *w = QApplication::widgetAt(QCursor::pos()); w; w = w->parentWidget()) {
				if (qobject_cast<QGraphicsView *>(w)) {
					// hide the window itself: hideText() lingers ~300 ms
					auto *tip = static_cast<QWidget *>(o);
					QTimer::singleShot(0, tip, [tip] { tip->hide(); });
					break;
				}
			}
		}
		if (auto *d = qobject_cast<QDialog *>(o)) {
			if (e->type() == QEvent::Show) {
				QTimer::singleShot(0, d, [this, d] {
					QJsonObject ev{{"event", "dialog_shown"}, {"title", d->windowTitle()},
					               {"class", d->metaObject()->className()},
					               {"box", box(d, d->rect())}};
					if (auto *mb = qobject_cast<QMessageBox *>(d))
						ev["text"] = mb->text();      // a message box says what it is in its text
					emitEvent(ev);
				});
			} else if (e->type() == QEvent::Hide) {
				emitEvent({{"event", "dialog_closed"}, {"title", d->windowTitle()}});
			}
		}
		return false;
	}

private:
	QLocalServer *m_server;
	QList<QLocalSocket *> m_subs;

	void emitEvent(const QJsonObject &o)
	{
		QJsonObject e = o;
		e["t"] = QDateTime::currentMSecsSinceEpoch() / 1000.0;
		const QByteArray line = QJsonDocument(e).toJson(QJsonDocument::Compact) + '\n';
		for (QLocalSocket *s : std::as_const(m_subs))
			s->write(line);
	}

	// Connect to actions, undo stacks and status bars as they appear.
	void scan()
	{
		for (QWidget *w : qApp->allWidgets()) {
			for (QAction *a : w->actions()) {
				if (a->property("_probed").toBool() || a->isSeparator())
					continue;
				a->setProperty("_probed", true);
				connect(a, &QAction::triggered, this, [this, a] {
					emitEvent({{"event", "action_triggered"}, {"text", plain(a->text())},
					           {"data", a->data().toString()}});
				});
			}
			if (auto *sb = qobject_cast<QStatusBar *>(w); sb && !sb->property("_probed").toBool()) {
				sb->setProperty("_probed", true);
				connect(sb, &QStatusBar::messageChanged, this, [this](const QString &m) {
					emitEvent({{"event", "status"}, {"text", m}});
				});
			}
		}
		for (QWidget *top : qApp->topLevelWidgets())
			for (QUndoStack *u : top->findChildren<QUndoStack *>()) {
				if (u->property("_probed").toBool())
					continue;
				u->setProperty("_probed", true);
				connect(u, &QUndoStack::indexChanged, this, [this, u](int i) {
					emitEvent({{"event", "undo"}, {"index", i}, {"text", u->undoText()},
					           {"redo_text", u->redoText()}});
				});
			}
	}

	void accept()
	{
		while (QLocalSocket *s = m_server->nextPendingConnection()) {
			connect(s, &QLocalSocket::readyRead, this, [this, s] { readLines(s); });
			connect(s, &QLocalSocket::disconnected, this, [this, s] { m_subs.removeAll(s); s->deleteLater(); });
		}
	}

	void readLines(QLocalSocket *s)
	{
		while (s->canReadLine()) {
			const QJsonObject req = QJsonDocument::fromJson(s->readLine()).object();
			QJsonObject ans = handle(req, s);
			if (req.contains("id"))
				ans["id"] = req["id"];
			s->write(QJsonDocument(ans).toJson(QJsonDocument::Compact) + '\n');
		}
	}

	// The graphics view showing a sheet: the biggest visible one.
	QGraphicsView *sheetView() const
	{
		QGraphicsView *best = nullptr;
		qint64 area = 0;
		for (QWidget *w : qApp->allWidgets())
			if (auto *v = qobject_cast<QGraphicsView *>(w); v && v->isVisible() && v->scene()) {
				const qint64 a = qint64(v->width()) * v->height();
				if (a > area) { best = v; area = a; }
			}
		return best;
	}

	QJsonObject handle(const QJsonObject &req, QLocalSocket *s)
	{
		const QString cmd = req["cmd"].toString();
		if (cmd == "ping")
			return {{"ok", true}, {"app", qApp->applicationName()}};
		if (cmd == "subscribe") {
			if (!m_subs.contains(s))
				m_subs << s;
			return {{"ok", true}};
		}
		if (cmd == "locate")
			return locate(req);
		if (cmd == "widget")
			return findWidgets(req);
		if (cmd == "combo_items")
			return comboItems();
		if (cmd == "view")
			return viewInfo();
		if (cmd == "scene_to_screen")
			return sceneToScreen(req);
		if (cmd == "frame")
			return frame(req);
		if (cmd == "scene_items")
			return sceneItems(req);
		if (cmd == "idle") {
			qApp->processEvents();
			return {{"ok", true}};
		}
		return {{"ok", false}, {"error", "unknown cmd " + cmd}};
	}

	// Where is the command with this text (or action data, or object name)?
	// "visible": only entries that can be clicked right now (open menus,
	// toolbar buttons, the menu bar). Without it, a closed menu's entries
	// come back with no box.
	QJsonObject locate(const QJsonObject &req)
	{
		const QString text = plain(req["text"].toString());
		const QString data = req["data"].toString();
		QJsonArray hits;
		for (QWidget *w : qApp->allWidgets()) {
			for (QAction *a : w->actions()) {
				if (a->isSeparator())
					continue;
				const bool match = (!text.isEmpty() && plain(a->text()) == text)
				    || (!data.isEmpty() && a->data().toString() == data);
				if (!match)
					continue;
				QJsonObject h{{"text", plain(a->text())}, {"data", a->data().toString()},
				              {"status_tip", a->statusTip()}, {"enabled", a->isEnabled()},
				              {"shortcut", a->shortcut().toString()}};
				if (auto *tb = qobject_cast<QToolBar *>(w)) {
					QWidget *b = tb->widgetForAction(a);
					if (!b || !b->isVisible())
						continue;
					h["kind"] = "toolbar";
					h["box"] = box(b, b->rect());
				} else if (auto *mb = qobject_cast<QMenuBar *>(w)) {
					if (!mb->isVisible())
						continue;
					h["kind"] = "menubar";
					h["box"] = box(mb, mb->actionGeometry(a));
				} else if (auto *m = qobject_cast<QMenu *>(w)) {
					h["kind"] = "menu";
					h["menu"] = plain(m->title());
					if (!m->isVisible()) {
						if (req["visible"].toBool())
							continue;
					} else {
						h["box"] = box(m, m->actionGeometry(a));
					}
					h["path"] = menuPath(m, a);
				} else {
					continue;
				}
				hits << h;
			}
		}
		return {{"ok", !hits.isEmpty()}, {"hits", hits}};
	}

	// Titles from the menu bar down to the action.
	QJsonArray menuPath(QMenu *m, QAction *a)
	{
		QStringList path{plain(a->text())};
		QMenu *cur = m;
		for (int guard = 0; cur && guard < 8; ++guard) {
			path.prepend(plain(cur->title()));
			// The menu bar wins: a menu can also sit in an untitled popup
			// (a toolbar's drop-down), which is not the way a person goes.
			QMenu *parent = nullptr;
			bool onBar = false;
			for (QWidget *w : qApp->allWidgets())
				for (QAction *pa : w->actions())
					if (pa->menu() == cur) {
						if (qobject_cast<QMenuBar *>(w))
							onBar = true;
						else if (!parent && qobject_cast<QMenu *>(w))
							parent = qobject_cast<QMenu *>(w);
					}
			cur = onBar ? nullptr : parent;
		}
		QJsonArray out;
		for (const QString &p : std::as_const(path))
			out << p;
		return out;
	}

	// The entries of a drop-down list that is open right now.
	QJsonObject comboItems()
	{
		QJsonArray hits;
		for (QWidget *w : qApp->allWidgets()) {
			auto *cb = qobject_cast<QComboBox *>(w);
			if (!cb || !cb->view() || !cb->view()->isVisible())
				continue;
			QAbstractItemView *v = cb->view();
			for (int row = 0; row < cb->count(); ++row) {
				const QRect r = v->visualRect(cb->model()->index(row, cb->modelColumn()));
				hits << QJsonObject{{"text", plain(cb->itemText(row))},
				                    {"box", box(v->viewport(), r)}};
			}
		}
		return {{"ok", !hits.isEmpty()}, {"hits", hits}};
	}

	QJsonObject findWidgets(const QJsonObject &req)
	{
		const QString cls = req["class"].toString(), name = req["name"].toString(),
		              title = req["title"].toString(), text = plain(req["text"].toString()),
		              contains = plain(req["contains"].toString());
		QJsonArray hits;
		for (QWidget *w : qApp->allWidgets()) {
			if (!w->isVisible() || w->width() < 2)
				continue;
			if (!cls.isEmpty() && !w->inherits(cls.toUtf8()))
				continue;
			if (!name.isEmpty() && w->objectName() != name)
				continue;
			if (!title.isEmpty() && !w->windowTitle().contains(title))
				continue;
			// a tab of a tab bar, or an entry of a list, by its text
			if (!text.isEmpty()) {
				if (auto *tb = qobject_cast<QTabBar *>(w)) {
					for (int i = 0; i < tb->count(); ++i)
						if (plain(tb->tabText(i)) == text)
							hits << QJsonObject{{"class", "QTabBar"}, {"name", w->objectName()},
							                    {"title", QString()}, {"text", tb->tabText(i)},
							                    {"box", box(tb, tb->tabRect(i))}};
					continue;
				}
				if (auto *lw = qobject_cast<QListWidget *>(w)) {
					for (int i = 0; i < lw->count(); ++i)
						if (plain(lw->item(i)->text()) == text)
							hits << QJsonObject{{"class", "QListWidget"}, {"name", w->objectName()},
							                    {"title", QString()}, {"text", lw->item(i)->text()},
							                    {"box", box(lw->viewport(), lw->visualItemRect(lw->item(i)))}};
					continue;
				}
			}
			if (!text.isEmpty()) {
				// a button by its text, a drop-down by what it shows now
				auto *b = qobject_cast<QAbstractButton *>(w);
				auto *cb = qobject_cast<QComboBox *>(w);
				if (!(b && plain(b->text()) == text) && !(cb && plain(cb->currentText()) == text))
					continue;
			}
			if (!contains.isEmpty()) {          // a button or label whose text contains this
				QString t;
				if (auto *b = qobject_cast<QAbstractButton *>(w))
					t = b->text();
				else if (auto *l = qobject_cast<QLabel *>(w))
					t = l->text();
				if (!plain(t).contains(contains))
					continue;
			}
			// a check box only takes a click on its indicator and text, not
			// on the empty width a layout gives it
			QRect hit_rect = w->rect();
			if (qobject_cast<QCheckBox *>(w))
				hit_rect.setWidth(qMin(hit_rect.width(), w->sizeHint().width()));
			hits << QJsonObject{{"class", w->metaObject()->className()}, {"name", w->objectName()},
			                    {"title", w->windowTitle()}, {"text", w->property("text").toString()},
			                    {"box", box(w, hit_rect)}};
		}
		return {{"ok", !hits.isEmpty()}, {"hits", hits}};
	}

	QJsonObject viewInfo()
	{
		QGraphicsView *v = sheetView();
		if (!v)
			return {{"ok", false}, {"error", "no sheet view"}};
		const QRectF vis = v->mapToScene(v->viewport()->rect()).boundingRect();
		return {{"ok", true}, {"zoom", v->transform().m11()},
		        {"visible", QJsonArray{vis.x(), vis.y(), vis.width(), vis.height()}},
		        {"viewport", box(v->viewport(), v->viewport()->rect())}};
	}

	QJsonObject sceneToScreen(const QJsonObject &req)
	{
		QGraphicsView *v = sheetView();
		if (!v)
			return {{"ok", false}, {"error", "no sheet view"}};
		const QPoint p = v->mapFromScene(QPointF(req["x"].toDouble(), req["y"].toDouble()));
		const qreal r = v->devicePixelRatioF();
		const QPoint g = v->viewport()->mapToGlobal(p);
		return {{"ok", true}, {"x", qRound(g.x() * r)}, {"y", qRound(g.y() * r)}};
	}

	// Zoom and centre so that scene rect [x,y,w,h] fills `fill` of the view.
	QJsonObject frame(const QJsonObject &req)
	{
		QGraphicsView *v = sheetView();
		if (!v)
			return {{"ok", false}, {"error", "no sheet view"}};
		const QJsonArray a = req["rect"].toArray();
		const QRectF rc(a[0].toDouble(), a[1].toDouble(), a[2].toDouble(), a[3].toDouble());
		const qreal fill = req["fill"].toDouble(0.7);
		v->fitInView(rc.adjusted(-rc.width() * (1 / fill - 1) / 2, -rc.height() * (1 / fill - 1) / 2,
		                         rc.width() * (1 / fill - 1) / 2, rc.height() * (1 / fill - 1) / 2),
		             Qt::KeepAspectRatio);
		v->centerOn(rc.center());
		return viewInfo();
	}

	QJsonObject sceneItems(const QJsonObject &req)
	{
		QGraphicsView *v = sheetView();
		if (!v)
			return {{"ok", false}, {"error", "no sheet view"}};
		const QString cls = req["class"].toString();
		QJsonArray out;
		for (QGraphicsItem *it : v->scene()->items(Qt::AscendingOrder)) {
			auto *obj = it->toGraphicsObject();
			const QString c = obj ? obj->metaObject()->className() : QString();
			if (c.isEmpty() || it->parentItem() || (!cls.isEmpty() && c != cls))
				continue;
			QJsonObject o{{"class", c}};
			const QRectF r = it->sceneBoundingRect();
			o["scene_rect"] = QJsonArray{r.x(), r.y(), r.width(), r.height()};
			if (c == QLatin1String("Conductor")) {
				// a point on the wire where a click selects it: the one
				// of its hit area, topmost there, nearest the middle of it
				QList<QPointF> hit;
				const QRectF br = it->sceneBoundingRect();
				for (qreal y = br.top(); y <= br.bottom(); y += 1)
					for (qreal x = br.left(); x <= br.right(); x += 1)
						if (it->contains(it->mapFromScene(QPointF(x, y)))
						    && v->scene()->itemAt(QPointF(x, y), QTransform()) == it)
							hit << QPointF(x, y);
				if (!hit.isEmpty()) {
					QPointF mid;
					for (const QPointF &q : hit)
						mid += q;
					mid /= hit.size();
					QPointF best = hit.first();
					for (const QPointF &q : hit)
						if (QLineF(q, mid).length() < QLineF(best, mid).length())
							best = q;
					o["on"] = QJsonArray{best.x(), best.y()};
				}
			}
			const QVariant line = obj->property("line");
			if (line.canConvert<QLineF>()) {
				const QPointF p1 = obj->mapToScene(line.toLineF().p1()), p2 = obj->mapToScene(line.toLineF().p2());
				o["p1"] = QJsonArray{p1.x(), p1.y()};
				o["p2"] = QJsonArray{p2.x(), p2.y()};
			}
			out << o;
		}
		return {{"ok", true}, {"items", out}};
	}
};

void startProbe()
{
	const QByteArray path = qgetenv("QET_DEMO_PROBE_SOCKET");
	if (path.isEmpty() || !qobject_cast<QApplication *>(qApp))
		return;
	new Probe(QString::fromLocal8Bit(path));
}

} // namespace

// Runs when the QApplication is constructed: the earliest point at which a
// preloaded library can create QObjects that live in the main thread.
static const int registered = (qAddPreRoutine([] { QTimer::singleShot(0, [] { startProbe(); }); }), 0);
