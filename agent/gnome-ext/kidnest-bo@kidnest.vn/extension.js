// Gọi điện - Nhắn tin con: exposes focus/idle/lock and an in-process screenshot on the session bus.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `<node>
  <interface name="vn.kidnest.Bo">
    <method name="Status"><arg type="s" direction="out" name="json"/></method>
    <method name="Screenshot"><arg type="s" direction="in" name="path"/><arg type="b" direction="out" name="ok"/></method>
  </interface>
</node>`;

class Service {
    Status() {
        const w = global.display.focus_window;
        const out = {
            idle_ms: global.backend.get_core_idle_monitor().get_idletime(),
            locked: Boolean(Main.screenShield?.locked),
        };
        if (w) {
            const app = Shell.WindowTracker.get_default().get_window_app(w);
            out.title = w.get_title() ?? '';
            out.wmClass = w.get_wm_class() ?? '';
            out.app = app ? app.get_name() : out.wmClass;
            out.fullscreen = w.is_fullscreen();
        }
        return JSON.stringify(out);
    }

    ScreenshotAsync([path], invocation) {
        try {
            const file = Gio.File.new_for_path(path);
            const stream = file.replace(null, false, Gio.FileCreateFlags.REPLACE_DESTINATION, null);
            const shooter = new Shell.Screenshot();
            shooter.screenshot(false, stream, (obj, res) => {
                try {
                    obj.screenshot_finish(res);
                    stream.close(null);
                    invocation.return_value(new GLib.Variant('(b)', [true]));
                } catch (e) {
                    invocation.return_dbus_error('vn.kidnest.Bo.Error', String(e));
                }
            });
        } catch (e) {
            invocation.return_dbus_error('vn.kidnest.Bo.Error', String(e));
        }
    }
}

export default class BoExtension extends Extension {
    enable() {
        this._service = new Service();
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this._service);
        this._dbus.export(Gio.DBus.session, '/vn/kidnest/Bo');
        this._name = Gio.bus_own_name_on_connection(Gio.DBus.session, 'vn.kidnest.Bo',
            Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
        if (this._name)
            Gio.bus_unown_name(this._name);
        this._name = 0;
        this._service = null;
    }
}
