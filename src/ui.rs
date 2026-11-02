use crate::theme::{self, Theme};
use anyhow::{Result, bail};
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind,
    },
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{prelude::*, widgets::*};
use serde_json::{Value, json};
use std::{
    io,
    path::Path,
    sync::mpsc,
    time::{Duration, Instant},
};

const TABS: [&str; 4] = ["Live", "Accounts", "Settings", "History"];
const TASKS: [(&str, &str); 4] = [
    ("account", "Account choices"),
    ("typing", "Typing"),
    ("math", "Math"),
    ("emoji", "Emoji"),
];
fn string<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}
fn num(v: &Value, k: &str) -> u64 {
    v.get(k).and_then(Value::as_u64).unwrap_or(0)
}
fn truth(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}
fn list<'a>(v: &'a Value, k: &str) -> &'a [Value] {
    v.get(k)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}
fn task_label(key: &str) -> &str {
    TASKS
        .iter()
        .find(|(k, _)| *k == key)
        .map(|(_, v)| *v)
        .unwrap_or(key)
}
fn value_text(v: &Value, k: &str, default: &str) -> String {
    match v.get(k) {
        Some(Value::String(s)) => s.clone(),
        Some(x) => x.to_string(),
        None => default.into(),
    }
}
fn money(v: &Value, key: &str) -> String {
    let n = value_text(v, key, "0").parse::<f64>().unwrap_or(0.);
    format!("{:.2}", n)
}
fn exact_rate(s: &Value) -> String {
    let n = num(s, "decided_submissions");
    if n == 0 {
        "--".into()
    } else {
        format!(
            "{:.1}%",
            num(&s["verdicts"], "correct") as f64 * 100. / n as f64
        )
    }
}

#[derive(Clone)]
struct Field {
    key: &'static str,
    label: String,
    value: String,
    password: bool,
    fresh: bool,
    multiline: bool,
    choices: Vec<&'static str>,
}
impl Field {
    fn text(key: &'static str, label: impl Into<String>, value: String) -> Self {
        Self {
            key,
            label: label.into(),
            value,
            password: false,
            fresh: true,
            multiline: false,
            choices: vec![],
        }
    }
    fn multiline(key: &'static str, label: impl Into<String>, value: String) -> Self {
        Self {
            key,
            label: label.into(),
            value,
            password: false,
            fresh: false,
            multiline: true,
            choices: vec![],
        }
    }
    fn choice(
        key: &'static str,
        label: impl Into<String>,
        value: String,
        choices: Vec<&'static str>,
    ) -> Self {
        Self {
            key,
            label: label.into(),
            value,
            password: false,
            fresh: false,
            multiline: false,
            choices,
        }
    }
    fn input(&mut self, c: char) {
        if self.multiline {
            if c == '\r' {
                return;
            }
            if c == '\n' {
                if self.value.len() < 65_536 {
                    self.value.push('\n');
                }
                return;
            }
            if c.is_control() {
                return;
            }
            if self.value.len() < 65_536 {
                self.value.push(c);
            }
            return;
        }
        if c.is_control() || !self.choices.is_empty() {
            return;
        }
        if self.key == "resume_time" && !c.is_ascii_digit() && c != ':' {
            return;
        }
        if self.fresh {
            self.value.clear();
            self.fresh = false;
        }
        if self.key == "resume_time" {
            let mut digits: String = self.value.chars().filter(char::is_ascii_digit).collect();
            if c == ':' {
                if digits.len() == 1 {
                    digits.insert(0, '0');
                    self.value = format!("{}:", digits);
                }
                return;
            }
            if digits.len() >= 4 {
                return;
            }
            digits.push(c);
            self.value = if digits.len() >= 2 {
                format!("{}:{}", &digits[..2], &digits[2..])
            } else {
                digits
            };
        } else if self.value.len() < 256 {
            self.value.push(c);
        }
    }
    fn cycle(&mut self, delta: isize) {
        if self.choices.is_empty() {
            return;
        }
        let i = self
            .choices
            .iter()
            .position(|v| *v == self.value)
            .unwrap_or(0);
        self.value = self.choices
            [(i as isize + delta).rem_euclid(self.choices.len() as isize) as usize]
            .into();
    }
}
struct Dialog {
    title: String,
    kind: &'static str,
    account_id: Option<String>,
    fields: Vec<Field>,
    focus: usize,
    note: String,
}
impl Dialog {
    fn focus_to(&mut self, index: usize) {
        self.focus = index;
        if let Some(field) = self.fields.get_mut(index) {
            field.fresh = true;
        }
    }
}
#[derive(Clone)]
enum Action {
    Tab(usize),
    Start(bool),
    Add,
    Edit,
    Check,
    Toggle,
    Remove,
    RefreshBalance,
    Details,
    HistoryBack,
    HistoryClear,
    HistoryTask,
    HistoryRuns,
    HistoryRun(usize),
    Pause(bool),
    Resume(bool),
    Stop(bool),
    Settings,
    Proxy,
    ProxyCheck,
    DialogField(usize),
    DialogSave,
    DialogCancel,
    Account(usize),
    AccountAction(usize, Box<Action>),
    History(usize),
    Quit,
}
struct App {
    snapshot: Value,
    tab: usize,
    selected: usize,
    history_selected: usize,
    theme: Theme,
    dialog: Option<Dialog>,
    message: String,
    error: bool,
    closing: bool,
    closed: bool,
    demo: bool,
    tx: Option<mpsc::Sender<String>>,
    actions: Vec<(Rect, Action)>,
    sequence: u64,
    pending_form: Option<u64>,
    pending_proxy: Option<u64>,
    updated: Instant,
    animation: Instant,
    expanded: bool,
    history_detail: bool,
    history_task: usize,
    history_runs: bool,
    run_selected: usize,
    pending_actions: Vec<(u64, String, String)>,
    seen_result: String,
}
impl App {
    fn new(tx: Option<mpsc::Sender<String>>, demo: bool) -> Self {
        Self {
            snapshot: if demo { demo_snapshot() } else { json!({}) },
            tab: 0,
            selected: 0,
            history_selected: 0,
            theme: theme::find("keycash"),
            dialog: None,
            message: if demo {
                "Preview mode".into()
            } else {
                "Loading embedded engine...".into()
            },
            error: false,
            closing: false,
            closed: false,
            demo,
            tx,
            actions: vec![],
            sequence: 0,
            pending_form: None,
            pending_proxy: None,
            updated: Instant::now(),
            animation: Instant::now(),
            expanded: false,
            history_detail: false,
            history_task: 0,
            history_runs: false,
            run_selected: 0,
            pending_actions: vec![],
            seen_result: String::new(),
        }
    }
    fn close_dialog(&mut self) {
        self.dialog = None;
    }
    fn accounts(&self) -> &[Value] {
        list(&self.snapshot, "accounts")
    }
    fn selected_account(&self) -> Option<&Value> {
        self.accounts().get(self.selected)
    }
    fn history_account(&self) -> &Value {
        list(&self.snapshot, "history")
            .get(self.history_selected)
            .unwrap_or(&Value::Null)
    }
    fn uid(&self) -> Option<String> {
        self.selected_account().map(|a| string(a, "id").to_owned())
    }
    fn send(&mut self, mut data: Value) -> Option<u64> {
        if self.demo {
            self.message = "Preview only; live account actions are disabled".into();
            return None;
        }
        let command = string(&data, "command").to_owned();
        let scope = string(&data, "account_id").to_owned();
        if self
            .pending_actions
            .iter()
            .any(|(_, c, a)| c == &command && a == &scope)
        {
            return None;
        }
        self.sequence += 1;
        let id = self.sequence;
        self.pending_actions.push((id, command, scope));
        data["id"] = json!(id);
        if let Some(tx) = &self.tx {
            if tx.send(data.to_string()).is_err() {
                self.message = "Engine is unavailable; restart the app".into();
                self.error = true;
                return None;
            }
        }
        Some(id)
    }
    fn notify(&mut self, message: &str) {
        self.message = message.into();
        self.error = false;
        self.updated = Instant::now();
    }
    fn paste(&mut self, text: &str) {
        if self.pending_form.is_some() {
            return;
        }
        let Some(dialog) = &mut self.dialog else {
            return;
        };
        let target = dialog
            .fields
            .get(dialog.focus)
            .filter(|field| field.choices.is_empty())
            .map(|_| dialog.focus)
            .or_else(|| dialog.fields.iter().position(|field| field.multiline))
            .or_else(|| {
                dialog
                    .fields
                    .iter()
                    .position(|field| field.choices.is_empty())
            });
        if let Some(index) = target
            && let Some(field) = dialog.fields.get_mut(index)
        {
            let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
            for c in normalized.chars() {
                field.input(c);
            }
        }
    }
    fn accept(&mut self, line: &str) -> Result<()> {
        let event: Value = serde_json::from_str(line)?;
        match string(&event, "type") {
            "snapshot" => {
                if self.message == "Loading embedded engine..." {
                    self.notify("Ready");
                }
                let newest = list(&event, "accounts")
                    .iter()
                    .filter_map(|a| {
                        let r = &a["last_result"];
                        if string(r, "verdict").is_empty() {
                            None
                        } else {
                            Some((
                                format!(
                                    "{}:{}:{}",
                                    string(a, "id"),
                                    num(r, "index"),
                                    string(r, "decided_at")
                                ),
                                string(r, "verdict").to_owned(),
                                string(r, "decided_at").to_owned(),
                                format!("{}: {}", string(a, "username"), result_message(r)),
                            ))
                        }
                    })
                    .max_by(|a, b| a.2.cmp(&b.2));
                if let Some((key, _verdict, _, feedback)) = newest {
                    if !self.seen_result.is_empty() && self.seen_result != key {
                        self.notify(&feedback);
                    }
                    self.seen_result = key;
                }
                if self.seen_result.is_empty() {
                    self.seen_result = "initial".into();
                }
                self.snapshot = event;
                self.theme = theme::find(string(&self.snapshot["settings"], "theme"));
                self.selected = self.selected.min(self.accounts().len().saturating_sub(1));
            }
            "proxy_text" => {
                if self.pending_proxy == event["id"].as_u64() {
                    self.pending_proxy = None;
                    self.proxy_dialog(string(&event, "text").to_owned(), truth(&event, "enabled"));
                    self.notify("Edit proxies, then save");
                }
            }
            "account_saved" => {
                if let Some(i) = self
                    .accounts()
                    .iter()
                    .position(|a| string(a, "id") == string(&event, "account_id"))
                {
                    self.selected = i;
                }
                if self.pending_form == event["id"].as_u64() {
                    self.dialog = None;
                    self.pending_form = None;
                    self.tab = 1;
                }
                self.notify("Account saved. Checking login...");
            }
            "result" => {
                self.pending_actions
                    .retain(|(id, _, _)| Some(*id) != event["id"].as_u64());
                if self.pending_proxy == event["id"].as_u64() {
                    self.pending_proxy = None;
                }
                if self.pending_form == event["id"].as_u64() && self.pending_form.is_some() {
                    self.pending_form = None;
                    if truth(&event, "ok") {
                        self.dialog = None;
                    } else if let Some(d) = &mut self.dialog {
                        d.note = string(&event, "message").into();
                        for field in &mut d.fields {
                            if field.password {
                                field.value.clear();
                            }
                        }
                    }
                }
                self.message = string(&event, "message").into();
                self.error = !truth(&event, "ok");
                self.updated = Instant::now();
            }
            "closed" => self.closed = true,
            "fatal" => bail!("{}", string(&event, "message")),
            _ => {}
        }
        Ok(())
    }
    fn account_dialog(&mut self, edit: bool) {
        let a = if edit {
            self.selected_account().cloned()
        } else {
            None
        };
        if edit && a.is_none() {
            self.notify("Add an account first");
            return;
        }
        let data = a.as_ref().unwrap_or(&Value::Null);
        let mut fields = vec![
            Field::text("username", "Username", value_text(data, "username", "")),
            Field::text("password", "Password", String::new()),
            Field::choice(
                "task",
                "Task",
                value_text(data, "task", "typing"),
                vec!["typing", "math", "emoji"],
            ),
            Field::text(
                "daily_target",
                "Daily target PHP (0 = no limit)",
                value_text(data, "daily_target", "0"),
            ),
            Field::choice(
                "enabled",
                "Include in Start all",
                value_text(data, "enabled", "true"),
                vec!["true", "false"],
            ),
        ];
        fields[1].password = true;
        self.dialog = Some(Dialog {
            title: if edit {
                "Edit account"
            } else {
                "Add account / sign in"
            }
            .into(),
            kind: "account",
            account_id: a.map(|x| string(&x, "id").to_string()),
            fields,
            focus: if edit { 1 } else { 0 },
            note: if edit {
                "Blank password keeps login. Running account: Stop & save ends its run."
            } else {
                "A target of 0 keeps solving until you stop it."
            }
            .into(),
        });
    }
    fn settings_dialog(&mut self) {
        let s = &self.snapshot["settings"];
        let fields = vec![
            Field::choice(
                "theme",
                "Theme",
                value_text(s, "theme", "keycash"),
                theme::THEMES.iter().map(|t| t.name).collect(),
            ),
            Field::text(
                "resume_time",
                "Daily resume time (HH:MM)",
                value_text(s, "resume_time", "08:00"),
            ),
            Field::text(
                "solve_delay_min",
                "Solve delay min (seconds)",
                value_text(s, "solve_delay_min", "25"),
            ),
            Field::text(
                "solve_delay_max",
                "Solve delay max (seconds)",
                value_text(s, "solve_delay_max", "40"),
            ),
            Field::choice(
                "rests_enabled",
                "Timed rests",
                value_text(s, "rests_enabled", "true"),
                vec!["true", "false"],
            ),
            Field::text(
                "work_min_minutes",
                "Work session min (minutes)",
                value_text(s, "work_min_minutes", "15"),
            ),
            Field::text(
                "work_max_minutes",
                "Work session max (minutes)",
                value_text(s, "work_max_minutes", "25"),
            ),
            Field::text(
                "rest_min_minutes",
                "Rest min (minutes)",
                value_text(s, "rest_min_minutes", "3"),
            ),
            Field::text(
                "rest_max_minutes",
                "Rest max (minutes)",
                value_text(s, "rest_max_minutes", "7"),
            ),
            Field::text(
                "network_retries",
                "Network retries (0-10)",
                value_text(s, "network_retries", "3"),
            ),
            Field::text(
                "retry_delay",
                "Retry delay seconds",
                value_text(s, "retry_delay", "5"),
            ),
            Field::choice(
                "on_unknown",
                "Unknown response",
                value_text(s, "on_unknown", "stop"),
                vec!["continue", "stop"],
            ),
        ];
        self.dialog = Some(Dialog {
            title: "Settings".into(),
            kind: "settings",
            account_id: None,
            fields,
            focus: 0,
            note: "Shared by all accounts. Ranges apply on the next interval.".into(),
        });
    }
    fn proxy_dialog(&mut self, text: String, enabled: bool) {
        let fields = vec![
            Field::choice(
                "proxy_enabled",
                "Use proxies",
                if enabled {
                    "true".into()
                } else {
                    "false".into()
                },
                vec!["true", "false"],
            ),
            Field::multiline(
                "proxy_text",
                "One per line: host:port:username:password",
                text,
            ),
        ];
        self.dialog = Some(Dialog {
            title: "Proxy setup".into(),
            kind: "proxies",
            account_id: None,
            fields,
            focus: 1,
            note: "One per line:\nhost:port:user:pass".into(),
        });
    }
    fn request_proxy_dialog(&mut self) {
        if self.demo {
            self.proxy_dialog(
                "proxy1.example.com:8000:user1:pass1\nproxy2.example.com:8000:user2:pass2\n".into(),
                true,
            );
            return;
        }
        if self.pending_proxy.is_some() {
            return;
        }
        if let Some(id) = self.send(json!({"command":"proxy_config"})) {
            self.pending_proxy = Some(id);
            self.notify("Loading proxy list...");
        }
    }

    fn confirm(&mut self, kind: &'static str) {
        let account = if kind == "clear_history" {
            Some(self.history_account())
        } else {
            self.selected_account()
        };
        if let Some(a) = account.filter(|a| !a.is_null()) {
            self.dialog = Some(Dialog {
                title: format!(
                    "{} {}?",
                    if kind == "clear_history" {
                        "Clear history for"
                    } else if kind == "remove" {
                        "Remove"
                    } else {
                        "Clear session for"
                    },
                    string(a, "username")
                ),
                kind,
                account_id: Some(string(a, "id").into()),
                fields: vec![],
                focus: 0,
                note: if kind == "clear_history" {
                    "All task history. Daily earnings and login stay."
                } else if kind == "remove" {
                    "Removes the account, saved password and cookies. Reports remain."
                } else {
                    "Clears cookies and the saved password. Edit to sign in again."
                }
                .into(),
            });
        }
    }
    fn save_dialog(&mut self) {
        if self.pending_form.is_some() {
            return;
        }
        let Some(d) = self.dialog.as_ref() else {
            return;
        };
        let mut values = serde_json::Map::new();
        for field in &d.fields {
            values.insert(
                field.key.into(),
                if matches!(
                    field.key,
                    "enabled" | "reduced_motion" | "rests_enabled" | "proxy_enabled"
                ) {
                    json!(field.value == "true")
                } else {
                    json!(&field.value)
                },
            );
        }
        let request = match d.kind {
            "account" => {
                values.insert("command".into(), json!("save_account"));
                values.insert("stop_and_save".into(), json!(true));
                if let Some(id) = &d.account_id {
                    values.insert("account_id".into(), json!(id));
                }
                Value::Object(values)
            }
            "settings" => json!({"command":"settings","settings":values}),
            "proxies" => {
                let enabled = values
                    .get("proxy_enabled")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                let text = values
                    .get("proxy_text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                json!({"command":"save_proxies","proxy_enabled":enabled,"proxy_text":text})
            }
            kind => {
                json!({"command":kind,"account_id":d.account_id,"confirmed":kind=="clear_history"})
            }
        };
        if self.demo {
            self.send(request);
            return;
        }
        let sent = self.send(request);
        self.pending_form = sent;
        if let Some(d) = &mut self.dialog {
            d.note = "Saving; please wait...".into();
        }
    }
    fn action(&mut self, action: Action) {
        if self.dialog.is_some() && self.pending_form.is_some() && !matches!(action, Action::Quit) {
            return;
        }
        if self.closing && !matches!(action, Action::Quit) {
            return;
        }
        if !matches!(action, Action::AccountAction(..)) && !enabled(self, &action) {
            self.notify("Action unavailable: check the account state or pending request");
            return;
        }
        match action {
            Action::Tab(i) => self.tab = i,
            Action::Start(selected) => {
                let id = if selected { self.uid() } else { None };
                if selected && id.is_none() {
                    self.notify("Add an account in Accounts first");
                    return;
                }
                self.send(json!({"command":"start","task":"account","account_id":id}));
                self.tab = 0;
            }
            Action::Add => self.account_dialog(false),
            Action::Edit => self.account_dialog(true),
            Action::Check => {
                if let Some(id) = self.uid() {
                    self.send(json!({"command":"check","account_id":id,"refresh":true}));
                }
            }
            Action::Toggle => {
                if let Some(id) = self.uid() {
                    self.send(json!({"command":"toggle","account_id":id}));
                }
            }
            Action::Remove => self.confirm("remove"),
            Action::RefreshBalance => {
                if let Some(id) = self.uid() {
                    self.send(json!({"command":"refresh_balance","account_id":id}));
                }
            }
            Action::Details => self.expanded = !self.expanded,
            Action::HistoryClear => self.confirm("clear_history"),
            Action::HistoryBack => {
                self.history_detail = false;
                self.history_runs = false;
            }
            Action::HistoryTask => {
                self.history_detail = true;
                self.history_task = (self.history_task + 1) % 4;
            }
            Action::HistoryRuns => {
                self.history_detail = true;
                self.history_runs = !self.history_runs;
                self.run_selected = 0;
            }
            Action::HistoryRun(i) => self.run_selected = i,
            Action::Pause(all) | Action::Resume(all) | Action::Stop(all) => {
                let command = match action {
                    Action::Pause(_) => "pause",
                    Action::Resume(_) => "resume",
                    _ => "stop",
                };
                let id = if all { None } else { self.uid() };
                if all || id.is_some() {
                    self.send(json!({"command":command,"account_id":id}));
                }
            }
            Action::Settings => self.settings_dialog(),
            Action::Proxy => self.request_proxy_dialog(),
            Action::ProxyCheck => {
                if self.demo {
                    self.notify("Preview only; proxy checks are disabled");
                } else if self.send(json!({"command":"check_proxies"})).is_some() {
                    self.notify("Checking proxies...");
                }
            }
            Action::DialogField(i) => {
                if let Some(d) = &mut self.dialog {
                    d.focus_to(i);
                    if let Some(field) = d.fields.get_mut(i) {
                        field.cycle(1);
                    }
                }
            }
            Action::DialogSave => self.save_dialog(),
            Action::DialogCancel => self.close_dialog(),
            Action::Account(i) => self.selected = i,
            Action::AccountAction(i, action) => {
                self.selected = i;
                self.action(*action);
            }
            Action::History(i) => {
                self.history_selected = i;
                self.history_detail = true;
                self.run_selected = 0;
            }
            Action::Quit => {
                if self.demo {
                    self.closed = true
                } else if !self.closing {
                    self.closing = true;
                    self.dialog = None;
                    self.notify("Stopping accounts; an in-flight answer may finish. Please wait.");
                    self.send(json!({"command":"shutdown"}));
                }
            }
        }
    }
    fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.action(Action::Quit);
            return;
        }
        if self.dialog.is_some()
            && key.modifiers.contains(KeyModifiers::CONTROL)
            && key.code == KeyCode::Char('s')
        {
            self.save_dialog();
            return;
        }
        if self.dialog.is_some() && self.pending_form.is_some() {
            return;
        }
        if self.dialog.is_some()
            && key.modifiers.contains(KeyModifiers::CONTROL)
            && key.code == KeyCode::Char('u')
        {
            if let Some(d) = &mut self.dialog {
                if let Some(field) = d.fields.get_mut(d.focus) {
                    if field.choices.is_empty() {
                        field.value.clear();
                        field.fresh = false;
                    }
                }
            }
            return;
        }
        if let Some(d) = &mut self.dialog {
            match key.code {
                KeyCode::Esc => self.close_dialog(),
                KeyCode::Enter => {
                    if d.fields.get(d.focus).is_some_and(|f| f.multiline) {
                        if let Some(field) = d.fields.get_mut(d.focus) {
                            field.input('\n');
                        }
                    } else if d.focus == d.fields.len() + 1 {
                        self.close_dialog();
                    } else if d.fields.is_empty() || d.focus + 1 >= d.fields.len() {
                        self.save_dialog()
                    } else {
                        d.focus_to(d.focus + 1);
                    }
                }
                KeyCode::Tab | KeyCode::Down => d.focus_to((d.focus + 1) % (d.fields.len() + 2)),
                KeyCode::BackTab | KeyCode::Up => {
                    d.focus_to((d.focus + d.fields.len() + 1) % (d.fields.len() + 2))
                }
                KeyCode::Left => {
                    if let Some(field) = d.fields.get_mut(d.focus) {
                        field.cycle(-1)
                    }
                }
                KeyCode::Right => {
                    if let Some(field) = d.fields.get_mut(d.focus) {
                        field.cycle(1)
                    }
                }
                KeyCode::Backspace => {
                    if let Some(field) = d.fields.get_mut(d.focus) {
                        if field.choices.is_empty() {
                            if field.fresh {
                                field.value.clear();
                                field.fresh = false;
                            } else {
                                field.value.pop();
                            }
                        }
                    }
                }
                KeyCode::Char(c) => {
                    if let Some(field) = d.fields.get_mut(d.focus) {
                        if field.choices.is_empty()
                            && !c.is_control()
                            && (field.fresh || field.multiline || field.value.len() < 256)
                        {
                            field.input(c)
                        } else if !field.choices.is_empty() && c == ' ' {
                            field.cycle(1)
                        }
                    }
                }
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Char(c) if ('1'..='4').contains(&c) => self.tab = (c as u8 - b'1') as usize,
            KeyCode::Tab => self.tab = (self.tab + 1) % 4,
            KeyCode::BackTab => self.tab = (self.tab + 3) % 4,
            KeyCode::Char('q') => self.action(Action::Quit),
            KeyCode::Up | KeyCode::Char('k') => {
                if self.tab == 3 {
                    if self.history_runs {
                        self.run_selected = self.run_selected.saturating_sub(1);
                    } else {
                        self.history_selected = self.history_selected.saturating_sub(1);
                    }
                } else {
                    self.selected = self.selected.saturating_sub(1);
                }
            }
            KeyCode::Down | KeyCode::Char('j') => {
                if self.tab == 3 {
                    if self.history_runs {
                        self.run_selected = (self.run_selected + 1)
                            .min(list(self.history_account(), "runs").len().saturating_sub(1));
                    } else {
                        self.history_selected = (self.history_selected + 1)
                            .min(list(&self.snapshot, "history").len().saturating_sub(1));
                    }
                } else {
                    self.selected =
                        (self.selected + 1).min(self.accounts().len().saturating_sub(1));
                }
            }
            KeyCode::Enter if self.tab == 3 => self.action(Action::History(self.history_selected)),
            KeyCode::Esc if self.tab == 3 => self.action(Action::HistoryBack),
            KeyCode::Char('t') if self.tab == 3 => self.action(Action::HistoryTask),
            KeyCode::Char('r') if self.tab == 3 => self.action(Action::HistoryRuns),
            KeyCode::Char('b') if self.tab == 0 || self.tab == 1 => {
                self.action(Action::RefreshBalance)
            }
            KeyCode::Char('d') if self.tab == 0 => self.action(Action::Details),
            KeyCode::Enter | KeyCode::Esc if self.tab == 0 && self.expanded => {
                self.action(Action::Details)
            }
            KeyCode::Enter if self.tab == 0 => {
                if let Some(a) = self.selected_account() {
                    let (_, primary, _) = live_control(a);
                    let action = Action::AccountAction(self.selected, Box::new(primary));
                    if enabled(self, &action) {
                        self.action(action);
                    }
                } else {
                    self.action(Action::Add);
                }
            }
            KeyCode::Char('s') if self.tab == 0 => self.action(Action::Start(false)),
            KeyCode::Char('p') if self.tab == 0 => self.action(Action::Pause(false)),
            KeyCode::Char('P') if self.tab == 0 => self.action(Action::Pause(true)),
            KeyCode::Char('r') if self.tab == 0 => self.action(Action::Resume(false)),
            KeyCode::Char('R') if self.tab == 0 => self.action(Action::Resume(true)),
            KeyCode::Char('x') if self.tab == 0 => self.action(Action::Stop(false)),
            KeyCode::Char('X') if self.tab == 0 => self.action(Action::Stop(true)),
            KeyCode::Char('a') => self.action(Action::Add),
            KeyCode::Char('e') if self.tab == 1 => self.action(Action::Edit),
            KeyCode::Char('v') if self.tab == 1 => self.action(Action::Check),
            KeyCode::Char(' ') if self.tab == 1 => self.action(Action::Toggle),
            KeyCode::Char('d') if self.tab == 1 => self.action(Action::Remove),
            KeyCode::Char('p') if self.tab == 2 => self.action(Action::Proxy),
            KeyCode::Char('c') if self.tab == 2 => self.action(Action::ProxyCheck),
            KeyCode::Enter | KeyCode::Char('e') if self.tab == 2 => self.action(Action::Settings),
            _ => {}
        }
    }
}
fn block<'a>(title: impl Into<Line<'a>>, theme: Theme) -> Block<'a> {
    Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(theme.border))
        .style(Style::default().bg(theme.background).fg(theme.foreground))
}
fn action_name(action: &Action) -> Option<(&'static str, bool)> {
    match action {
        Action::Start(selected) => Some(("start", !*selected)),
        Action::Pause(all) => Some(("pause", *all)),
        Action::Resume(all) => Some(("resume", *all)),
        Action::Stop(all) => Some(("stop", *all)),
        Action::Edit => Some(("edit", false)),
        Action::Check => Some(("check", false)),
        Action::Remove => Some(("remove", false)),
        Action::RefreshBalance => Some(("refresh_balance", false)),
        _ => None,
    }
}
fn account_eligible(a: &Value, command: &str) -> bool {
    if let Some(flag) = a["actions"][command].as_bool() {
        return flag;
    }
    let active = truth(a, "active");
    let busy = truth(a, "busy");
    let mode = string(a, "schedule_mode");
    if busy || string(a, "status") == "Stopping" {
        return false;
    }
    match command {
        "start" => {
            !active
                && mode != "paused"
                && mode != "waiting"
                && (truth(a, "login_saved") || truth(a, "session_saved"))
        }
        "pause" => (active || truth(a, "scheduled")) && mode != "paused",
        "resume" => mode == "paused",
        "stop" => active || truth(a, "scheduled"),
        "check" => !active && truth(a, "login_saved"),
        _ => true,
    }
}
fn enabled(app: &App, action: &Action) -> bool {
    if matches!(action, Action::HistoryClear) {
        return !app.history_account().is_null()
            && !app.pending_actions.iter().any(|(_, c, uid)| {
                c == "clear_history" && uid == string(app.history_account(), "id")
            });
    }
    if let Action::AccountAction(i, inner) = action {
        return app.accounts().get(*i).is_some_and(|a| {
            action_name(inner).is_some_and(|(c, _)| {
                account_eligible(a, c)
                    && !app
                        .pending_actions
                        .iter()
                        .any(|(_, _, uid)| uid == string(a, "id"))
            })
        });
    }
    if matches!(action, Action::Edit) {
        return app.selected_account().is_some();
    }
    if let Some((command, all)) = action_name(action) {
        if all {
            return !app
                .pending_actions
                .iter()
                .any(|(_, c, uid)| c == command && uid.is_empty())
                && app.accounts().iter().any(|a| {
                    (command != "start" || truth(a, "enabled")) && account_eligible(a, command)
                });
        }
        return app.selected_account().is_some_and(|a| {
            account_eligible(a, command)
                && !app
                    .pending_actions
                    .iter()
                    .any(|(_, _, uid)| uid == string(a, "id"))
        });
    }
    match action {
        Action::Toggle => app
            .selected_account()
            .is_some_and(|a| !truth(a, "active") && !truth(a, "busy")),
        Action::DialogSave | Action::DialogCancel => app.pending_form.is_none(),
        _ => true,
    }
}
fn button(f: &mut Frame, app: &mut App, rect: Rect, label: &str, action: Action) {
    if rect.is_empty() {
        return;
    }
    let t = app.theme;
    let on = enabled(app, &action);
    let inner = if let Action::AccountAction(_, inner) = &action {
        inner.as_ref()
    } else {
        &action
    };
    let color = match inner {
        Action::Start(_) | Action::Resume(_) => t.success,
        Action::Pause(_) => t.warning,
        Action::Stop(_) | Action::Remove | Action::HistoryClear => t.error,
        Action::DialogSave
            if app
                .dialog
                .as_ref()
                .is_some_and(|d| matches!(d.kind, "remove" | "clear_history")) =>
        {
            t.error
        }
        Action::DialogSave
            if app
                .dialog
                .as_ref()
                .and_then(|d| d.account_id.as_ref())
                .and_then(|uid| app.accounts().iter().find(|a| string(a, "id") == uid))
                .is_some_and(|a| truth(a, "active")) =>
        {
            t.error
        }
        Action::DialogCancel | Action::Check | Action::RefreshBalance => t.muted,
        _ => t.accent,
    };
    let focused = app.dialog.as_ref().is_some_and(|d| match action {
        Action::DialogSave => d.focus == d.fields.len(),
        Action::DialogCancel => d.focus == d.fields.len() + 1,
        _ => false,
    });
    let style = if on {
        Style::default().fg(t.background).bg(color).bold()
    } else {
        Style::default().fg(t.muted).bg(t.focus_background)
    };
    f.render_widget(Block::default().style(style), rect);
    let y = rect.y + rect.height.saturating_sub(1) / 2;
    f.render_widget(
        Paragraph::new(label)
            .alignment(Alignment::Center)
            .style(if focused {
                style.add_modifier(Modifier::UNDERLINED)
            } else {
                style
            }),
        Rect::new(rect.x, y, rect.width, 1),
    );
    if on {
        app.actions.push((rect, action));
    }
}
fn inline_actions(f: &mut Frame, app: &mut App, rect: Rect, actions: Vec<(&str, Action)>) {
    let labels: usize = actions.iter().map(|(label, _)| label.chars().count()).sum();
    let gaps = actions.len().saturating_sub(1);
    let padding = if labels + gaps + actions.len() * 2 <= rect.width as usize {
        2
    } else {
        1
    };
    let mut x = rect.x;
    for (label, action) in actions {
        let w = (label.chars().count() as u16 + padding).min(rect.right().saturating_sub(x));
        if w == 0 {
            break;
        }
        button(f, app, Rect::new(x, rect.y, w, 1), label, action);
        x += w + 1;
    }
}
fn live_control(a: &Value) -> (&'static str, Action, bool) {
    let armed = truth(a, "active") || truth(a, "scheduled");
    if string(a, "status") == "Stopping" {
        ("Wait", Action::Pause(false), true)
    } else if string(a, "schedule_mode") == "paused" {
        ("Resume", Action::Resume(false), armed)
    } else if armed {
        ("Pause", Action::Pause(false), true)
    } else {
        ("Start", Action::Start(true), false)
    }
}
fn header_status(app: &App, width: u16) -> [Line<'static>; 3] {
    let t = app.theme;
    let paused = app
        .accounts()
        .iter()
        .filter(|a| string(a, "schedule_mode") == "paused")
        .count();
    let is_running = |a: &&Value| {
        truth(a, "active")
            && !["paused", "waiting", "stopped"].contains(&string(a, "schedule_mode"))
            && string(a, "status") != "Stopping"
            && string(a, "wait_kind") != "Resting"
    };
    let running = app.accounts().iter().filter(is_running).count();
    let retrying = app
        .accounts()
        .iter()
        .filter(is_running)
        .filter(|a| truth(a, "recovering"))
        .count();
    let counts = if app.accounts().is_empty() {
        Line::styled("0 accounts", Style::default().fg(t.muted))
    } else {
        let full = format!("{} running · {} paused", running, paused);
        let short = format!("{} run · {} pause", running, paused);
        let (r, separator, p) = if full.len() <= width as usize {
            (
                format!("{} running", running),
                " · ",
                format!("{} paused", paused),
            )
        } else if short.len() <= width as usize {
            (
                format!("{} run", running),
                " · ",
                format!("{} pause", paused),
            )
        } else {
            (format!("{}R", running), " ", format!("{}P", paused))
        };
        Line::from(vec![
            Span::styled(
                r,
                Style::default().fg(if running > 0 { t.success } else { t.muted }),
            ),
            Span::styled(separator, Style::default().fg(t.muted)),
            Span::styled(
                p,
                Style::default().fg(if paused > 0 { t.warning } else { t.muted }),
            ),
        ])
    };
    let recovery = if retrying == 0 {
        "Ready".into()
    } else if format!("{} retrying", retrying).len() < width as usize {
        format!("{} retrying", retrying)
    } else {
        format!("{} retry", retrying)
    };
    let resting = app
        .accounts()
        .iter()
        .filter(|a| {
            truth(a, "active")
                && string(a, "schedule_mode") == "running"
                && string(a, "wait_kind") == "Resting"
        })
        .count();
    let accounts = app.accounts().len();
    let total = if resting > 0 {
        if width < 8 {
            format!("{}rest", resting)
        } else {
            format!("{} resting", resting)
        }
    } else if width < 8 {
        format!("{}ac", accounts)
    } else {
        format!("{} accounts", accounts)
    };
    let recovery = if recovery.chars().count() > width as usize {
        format!("↻{}", retrying)
    } else {
        recovery
    };
    [
        counts,
        Line::styled(
            recovery,
            Style::default().fg(if retrying > 0 { t.warning } else { t.muted }),
        ),
        Line::styled(total, Style::default().fg(t.muted)),
    ]
}

fn draw(f: &mut Frame, app: &mut App) {
    app.actions.clear();
    let area = f.area();
    let t = app.theme;
    f.render_widget(
        Block::default().style(Style::default().bg(t.background).fg(t.foreground)),
        area,
    );
    if area.width < 28 || area.height < 14 {
        f.render_widget(
            Paragraph::new("Use 28+ columns and 14+ rows. q exits."),
            area,
        );
        return;
    }
    let regions = Layout::vertical([
        Constraint::Length(4),
        Constraint::Min(5),
        Constraint::Length(2),
        Constraint::Length(1),
    ])
    .split(area);
    let header = regions[0];
    let letters = [
        ["█ ▄", "█▀▄", "█ █"],
        ["█▀▀", "█▀▀", "▀▀▀"],
        ["█ █", "▀█▀", " █ "],
        ["█▀▀", "█  ", "▀▀▀"],
        ["▄▀▄", "█▀█", "█ █"],
        ["█▀▀", "▀▀█", "▀▀▀"],
        ["█ █", "█▀█", "█ █"],
    ];
    let scale = if area.width >= 80 { 2 } else { 1 };
    let gap = if area.width >= 40 { " " } else { "" };
    let art: Vec<String> = (0..3)
        .map(|row| {
            letters
                .iter()
                .map(|letter| {
                    letter[row]
                        .chars()
                        .map(|c| c.to_string().repeat(scale))
                        .collect::<String>()
                })
                .collect::<Vec<_>>()
                .join(gap)
        })
        .collect();
    let logo_width = art[0].chars().count() as u16;
    let lines: Vec<Line> = art
        .into_iter()
        .enumerate()
        .map(|(row, text)| {
            Line::styled(
                text,
                Style::default()
                    .fg(if row == 1 { t.foreground } else { t.accent })
                    .bold(),
            )
        })
        .collect();
    f.render_widget(
        Paragraph::new(lines),
        Rect::new(header.x, header.y, logo_width, 3),
    );
    let status_x = logo_width + 2;
    let status_width = header.width.saturating_sub(status_x);
    f.render_widget(
        Paragraph::new(header_status(app, status_width).to_vec()).alignment(Alignment::Right),
        Rect::new(header.x + status_x, header.y, status_width, 3),
    );
    f.render_widget(
        Paragraph::new(format!(
            "Keycash Pro v{} by xnote12",
            env!("CARGO_PKG_VERSION")
        ))
        .style(Style::default().fg(t.muted)),
        Rect::new(header.x, header.y + 3, header.width, 1),
    );
    let wide = area.width >= 110 && area.height >= 28;
    let (body, nav) = if wide {
        let p = Layout::horizontal([Constraint::Length(18), Constraint::Min(30)])
            .spacing(2)
            .split(Rect::new(
                regions[1].x,
                regions[1].y,
                regions[1].width,
                regions[1].height + regions[2].height,
            ));
        (p[1], p[0])
    } else {
        (
            regions[1],
            Rect::new(regions[2].x, regions[2].bottom() - 1, regions[2].width, 1),
        )
    };
    if wide {
        for (i, title) in TABS.iter().enumerate() {
            let r = Rect::new(nav.x, nav.y + i as u16 * 2, nav.width, 1);
            f.render_widget(
                Paragraph::new(format!(
                    " {} {}",
                    if i == app.tab { ">" } else { " " },
                    title
                ))
                .style(if i == app.tab {
                    Style::default().fg(t.accent).bg(t.focus_background).bold()
                } else {
                    Style::default().fg(t.muted)
                }),
                r,
            );
            app.actions.push((r, Action::Tab(i)));
        }
    } else {
        let cells = Layout::horizontal([Constraint::Ratio(1, 4); 4]).split(nav);
        for (i, title) in (if area.width < 38 {
            ["Live", "Accts", "Setup", "History"]
        } else {
            ["Live", "Accounts", "Settings", "History"]
        })
        .iter()
        .enumerate()
        {
            f.render_widget(
                Paragraph::new(*title)
                    .alignment(Alignment::Center)
                    .style(if i == app.tab {
                        Style::default().fg(t.accent).bg(t.focus_background).bold()
                    } else {
                        Style::default().fg(t.muted)
                    }),
                cells[i],
            );
            app.actions.push((cells[i], Action::Tab(i)));
        }
    }
    let body = body.inner(Margin::new(1, 0));
    match app.tab {
        0 => solve(f, app, body),
        1 => accounts(f, app, body),
        2 => settings(f, app, body),
        _ => history(f, app, body),
    }
    f.render_widget(
        Paragraph::new(if app.closing {
            "Stopping safely…"
        } else {
            &app.message
        })
        .style(Style::default().fg(if app.error { t.error } else { t.muted })),
        regions[3],
    );
    if app.dialog.is_some() {
        app.actions.clear();
        dialog(f, app, area)
    }
}
fn text_row(f: &mut Frame, r: Rect, text: impl Into<String>, color: Color) {
    if r.height > 0 {
        f.render_widget(
            Paragraph::new(text.into()).style(Style::default().fg(color)),
            Rect::new(r.x, r.y, r.width, 1),
        );
    }
}
fn balance_label(a: &Value) -> String {
    if a["balance"].is_object() {
        format!(
            "₱{}{}",
            string(&a["balance"], "amount"),
            if truth(&a["balance"], "stale") {
                " · stale"
            } else {
                ""
            }
        )
    } else {
        "—".into()
    }
}
fn countdown_color(t: Theme, remaining: u64, total: f64) -> Color {
    if remaining <= 3 {
        return t.success;
    }
    let fraction = (remaining as f64 / total.max(1.)).clamp(0., 1.);
    let (from, to, amount) = if fraction > 0.5 {
        (t.accent, t.warning, (1. - fraction) * 2.)
    } else {
        (t.warning, t.success, 1. - fraction * 2.)
    };
    if let (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) = (from, to) {
        let mix = |a: u8, b: u8| (a as f64 + (b as f64 - a as f64) * amount).round() as u8;
        Color::Rgb(mix(r1, r2), mix(g1, g2), mix(b1, b2))
    } else if remaining > 10 {
        t.accent
    } else {
        t.warning
    }
}
fn payout_suffix(r: &Value) -> String {
    let amount = value_text(r, "earned_points", "0")
        .parse::<f64>()
        .unwrap_or(0.);
    if amount.is_finite() && amount > 0. {
        format!(" +₱{}", money(r, "earned_points"))
    } else {
        String::new()
    }
}
fn answer_suffix(r: &Value) -> String {
    let answer = string(r, "answer").trim();
    if answer.is_empty() {
        String::new()
    } else {
        format!(" {answer}")
    }
}
fn activity_dots(app: &App) -> &'static str {
    [".", "..", "..."][((app.animation.elapsed().as_millis() / 350) % 3) as usize]
}
fn verification_text(width: u16, dots: &str) -> String {
    let width = width as usize;
    if width >= 30 {
        format!("Solving human verification{dots}")
    } else if width >= 20 {
        format!("Human verification{dots}")
    } else if width >= 12 {
        format!("Verifying{dots}")
    } else {
        format!("Verify{dots}")
    }
}
fn result_message(r: &Value) -> String {
    let i = num(r, "index") as usize;
    match string(r, "verdict") {
        "correct" => format!(
            "✓ {}{}{}",
            ["Nailed it!", "Solved!", "Got it!"][i % 3],
            answer_suffix(r),
            payout_suffix(r)
        ),
        "incorrect" => format!(
            "✗ {}{}",
            ["Missed that one.", "That one slipped.", "A tricky one."][i % 3],
            answer_suffix(r)
        ),
        "" => "No result yet".into(),
        _ => "? Result unconfirmed".into(),
    }
}
fn rest_timer(seconds: u64) -> String {
    format!("{:02}:{:02}", seconds / 60, seconds % 60)
}
fn progress_label(a: &Value) -> String {
    let current = a
        .get("batch_points")
        .filter(|v| v.is_u64())
        .map(|v| v.to_string())
        .unwrap_or("—".into());
    let size = a
        .get("batch_size")
        .filter(|v| v.as_u64().is_some_and(|n| n > 0))
        .map(|v| v.to_string())
        .unwrap_or("?".into());
    format!("{}/{}", current, size)
}
fn has_target(a: &Value) -> bool {
    value_text(a, "daily_target", "0")
        .parse::<f64>()
        .unwrap_or(0.)
        > 0.
}
fn daily_limit(a: &Value) -> String {
    if has_target(a) {
        format!("₱{}/day", money(a, "daily_target"))
    } else {
        "No daily limit".into()
    }
}
fn wallet_progress(a: &Value, width: u16) -> String {
    let compact = |key| {
        money(a, key)
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_owned()
    };
    let daily = if has_target(a) {
        format!("₱{}/{}", compact("daily_points"), compact("daily_target"))
    } else {
        format!("₱{}", compact("daily_points"))
    };
    let full = format!("Wallet: {} · Today {}", balance_label(a), daily);
    if full.chars().count() <= width as usize {
        full
    } else {
        format!("Wallet {} · {}", balance_label(a), daily)
    }
}
fn recent_solves(f: &mut Frame, a: &Value, rect: Rect, t: Theme) {
    if rect.height == 0 {
        return;
    }
    text_row(
        f,
        rect,
        if rect.width < 28 {
            "Last 5 · newest first"
        } else {
            "Last 5 solves · newest first"
        },
        t.muted,
    );
    let rows = list(a, "recent_solves");
    if rows.is_empty() {
        if rect.height > 1 {
            text_row(
                f,
                Rect::new(rect.x, rect.y + 1, rect.width, 1),
                "No submissions yet",
                t.muted,
            );
        }
        return;
    }
    let status = |r: &Value| match string(r, "verdict") {
        "correct" => ("✓", "Correct", t.success),
        "incorrect" => ("✗", "Wrong", t.error),
        _ => ("?", "Unknown", t.warning),
    };
    if rect.height < 6 {
        if rect.height > 1 {
            let spans: Vec<Span> = rows
                .iter()
                .take(5)
                .map(|r| {
                    let (mark, _, color) = status(r);
                    Span::styled(format!(" {} ", mark), Style::default().fg(color).bold())
                })
                .collect();
            f.render_widget(
                Paragraph::new(Line::from(spans)),
                Rect::new(rect.x, rect.y + 1, rect.width, 1),
            );
        }
        if rect.height > 2 {
            text_row(
                f,
                Rect::new(rect.x, rect.y + 2, rect.width, 1),
                "✓ OK  ✗ Wrong  ? Unknown",
                t.muted,
            );
        }
        return;
    }
    for (i, r) in rows.iter().take(5).enumerate() {
        let (mark, label, color) = status(r);
        let suffix = if string(r, "verdict") == "correct" {
            payout_suffix(r)
        } else {
            String::new()
        };
        let answer = string(r, "answer").trim();
        let detail = if answer.is_empty() { label } else { answer };
        text_row(
            f,
            Rect::new(rect.x, rect.y + 1 + i as u16, rect.width, 1),
            format!(
                "{} {} {} {}{}",
                mark,
                string(r, "time_local"),
                task_label(string(r, "task")),
                detail,
                suffix
            ),
            color,
        );
    }
}
fn solve(f: &mut Frame, app: &mut App, rect: Rect) {
    let t = app.theme;
    let dots = activity_dots(app);
    if app.accounts().is_empty() {
        text_row(f, rect, "Add an account to start solving.", t.muted);
        button(
            f,
            app,
            Rect::new(rect.x, rect.y + 2, 16, 1),
            "+ Add account",
            Action::Add,
        );
        return;
    }
    if app.expanded && rect.height < 15 {
        let a = app.selected_account().unwrap().clone();
        text_row(f, rect, string(&a, "username"), t.accent);
        text_row(
            f,
            Rect::new(rect.x, rect.y + 1, rect.width, 1),
            format!("Wallet: {}", balance_label(&a)),
            t.foreground,
        );
        text_row(
            f,
            Rect::new(rect.x, rect.y + 2, rect.width, 1),
            if has_target(&a) {
                format!(
                    "{} {} · ₱{}/{}",
                    task_label(string(&a, "task")),
                    progress_label(&a),
                    money(&a, "daily_points"),
                    money(&a, "daily_target")
                )
            } else {
                format!(
                    "{} {} · ₱{} today",
                    task_label(string(&a, "task")),
                    progress_label(&a),
                    money(&a, "daily_points")
                )
            },
            t.success,
        );
        recent_solves(
            f,
            &a,
            Rect::new(
                rect.x,
                rect.y + 3,
                rect.width,
                rect.height.saturating_sub(4),
            ),
            t,
        );
        inline_actions(
            f,
            app,
            Rect::new(rect.x, rect.bottom() - 1, rect.width, 1),
            vec![
                ("Back", Action::Details),
                ("Refresh ₱", Action::RefreshBalance),
            ],
        );
        return;
    }
    text_row(f, rect, "All accounts", t.muted);
    let slots = Layout::horizontal([
        Constraint::Min(5),
        Constraint::Min(5),
        Constraint::Min(6),
        Constraint::Min(4),
    ])
    .spacing(1)
    .split(Rect::new(rect.x, rect.y + 1, rect.width, 1));
    for (i, (label, action)) in [
        ("Start", Action::Start(false)),
        ("Pause", Action::Pause(true)),
        ("Resume", Action::Resume(true)),
        ("Stop", Action::Stop(true)),
    ]
    .into_iter()
    .enumerate()
    {
        button(f, app, slots[i], label, action);
    }
    let detail_h = if app.expanded && rect.height >= 15 {
        6
    } else {
        0
    };
    let body = Rect::new(
        rect.x,
        rect.y + 3,
        rect.width,
        rect.height.saturating_sub(4 + detail_h),
    );
    let visible = (body.height / 3).max(1) as usize;
    let first = app.selected.saturating_sub(visible - 1);
    for i in first..(first + visible).min(app.accounts().len()) {
        let a = app.accounts()[i].clone();
        let y = body.y + (i - first) as u16 * 3;
        if y + 2 >= body.bottom() {
            break;
        }
        let r = Rect::new(body.x, y, body.width, 3);
        let selected = i == app.selected;
        if selected {
            f.render_widget(
                Block::default().style(
                    Style::default()
                        .bg(t.focus_background)
                        .fg(t.selection_foreground),
                ),
                r,
            );
        }
        let name_w = r.width.saturating_sub(14);
        app.actions
            .push((Rect::new(r.x, r.y, name_w, 1), Action::Account(i)));
        app.actions
            .push((Rect::new(r.x, r.y + 1, r.width, 2), Action::Account(i)));
        text_row(
            f,
            Rect::new(r.x, r.y, name_w, 1),
            string(&a, "username"),
            if selected { t.accent } else { t.foreground },
        );
        let (label, primary, stop) = live_control(&a);
        button(
            f,
            app,
            Rect::new(r.right() - 13, r.y, 7, 1),
            label,
            Action::AccountAction(i, Box::new(primary)),
        );
        button(
            f,
            app,
            Rect::new(r.right() - 5, r.y, 5, 1),
            "Stop",
            Action::AccountAction(i, Box::new(Action::Stop(false))),
        );
        let _ = stop;
        text_row(
            f,
            Rect::new(r.x, r.y + 1, r.width, 1),
            wallet_progress(&a, r.width),
            t.foreground,
        );
        let answer = {
            let raw = {
                let answer = string(&a["last"], "answer").trim();
                if answer.is_empty() {
                    string(&a["last"], "prediction").trim()
                } else {
                    answer
                }
            };
            if raw.is_empty() || raw == "—" {
                "—".to_owned()
            } else {
                let mark = match string(&a["last"], "verdict") {
                    "correct" => "✓ ",
                    "incorrect" => "✗ ",
                    _ => "",
                };
                format!("{mark}{raw}")
            }
        };
        let verifying = string(&a, "activity") == "verification";
        let status = if !string(&a, "error").is_empty() {
            string(&a, "error").to_owned()
        } else if matches!(
            string(&a, "status"),
            "Stopping" | "Pausing" | "Pause after current" | "Paused"
        ) {
            string(&a, "status").to_owned()
        } else if verifying {
            verification_text(r.width.saturating_sub(12), dots)
        } else if string(&a, "status").contains("/ retry") {
            string(&a, "status").to_owned()
        } else if string(&a, "wait_kind") == "Resting" && num(&a, "remaining") > 0 {
            format!(
                "{} {}",
                if string(&a, "schedule_mode") == "paused" {
                    "Paused · Rest"
                } else {
                    "Resting"
                },
                rest_timer(num(&a, "remaining"))
            )
        } else if string(&a, "schedule_mode") == "waiting" {
            format!("Resumes {}", string(&a, "next_resume"))
        } else if num(&a, "remaining") > 0 && truth(&a, "active") {
            format!("{}s", num(&a, "remaining"))
        } else {
            string(&a, "status").to_owned()
        };
        let task = task_label(string(&a, "task"));
        let short = match string(&a, "task") {
            "typing" => "Type",
            _ => task,
        };
        let points = progress_label(&a);
        let mut shown_task = task;
        let shown_points = points.clone();
        let mut shown_status = status.clone();
        let full_line = |task: &str, points: &str, status: &str| {
            format!("{} {} · {} · {}", task, points, answer, status)
        };
        let mut show_progress =
            full_line(task, &points, &status).chars().count() <= r.width as usize;
        if !show_progress && !verifying {
            shown_task = short;
            show_progress = full_line(shown_task, &shown_points, &shown_status)
                .chars()
                .count()
                <= r.width as usize;
        }
        if !show_progress && verifying {
            let available = r.width.saturating_sub(answer.chars().count() as u16 + 4);
            shown_status = verification_text(available, dots);
        }
        let mut spans = Vec::new();
        if show_progress {
            spans.push(Span::styled(
                format!("{} ", shown_task),
                Style::default().fg(t.accent),
            ));
            spans.push(Span::styled(shown_points, Style::default().fg(t.success)));
            spans.push(Span::styled(" · ", Style::default().fg(t.border)));
            spans.push(Span::styled(
                answer.clone(),
                Style::default().fg(t.foreground),
            ));
            spans.push(Span::styled(" · ", Style::default().fg(t.border)));
        } else if string(&a, "error").is_empty() {
            spans.push(Span::styled(
                answer.clone(),
                Style::default().fg(t.foreground),
            ));
            spans.push(Span::styled(" · ", Style::default().fg(t.border)));
        }
        let timer = format!("{}s", num(&a, "remaining"));
        let countdown = !verifying
            && num(&a, "remaining") > 0
            && truth(&a, "active")
            && string(&a, "wait_kind") != "Resting"
            && shown_status.ends_with(&timer)
            && string(&a, "error").is_empty()
            && !matches!(
                string(&a, "status"),
                "Stopping" | "Pausing" | "Pause after current" | "Paused"
            );
        if countdown {
            let prefix = shown_status.strip_suffix(&timer).unwrap();
            let reading = prefix.trim_end_matches(" · ").trim_end();
            spans.push(Span::styled(
                reading.to_owned(),
                Style::default().fg(t.foreground).bold(),
            ));
            spans.push(Span::styled(
                prefix[reading.len()..].to_owned(),
                Style::default().fg(t.border),
            ));
            let total = a["wait_total"].as_f64().unwrap_or(40.);
            spans.push(Span::styled(
                timer,
                Style::default()
                    .fg(countdown_color(t, num(&a, "remaining"), total))
                    .bold(),
            ));
        } else {
            spans.push(Span::styled(
                shown_status,
                Style::default().fg(if !string(&a, "error").is_empty() {
                    t.error
                } else if verifying {
                    t.accent
                } else if string(&a, "status").contains("/ retry")
                    || string(&a, "wait_kind") == "Resting"
                {
                    t.warning
                } else {
                    t.foreground
                }),
            ));
        }
        f.render_widget(
            Paragraph::new(Line::from(spans)),
            Rect::new(r.x, r.y + 2, r.width, 1),
        );
    }
    let cards_end = body.y + ((first + visible).min(app.accounts().len()) - first) as u16 * 3;
    let controls = Rect::new(
        rect.x,
        (cards_end + 1).min(rect.bottom() - 1 - detail_h),
        rect.width,
        1,
    );
    inline_actions(
        f,
        app,
        controls,
        vec![
            (
                if app.expanded { "Less" } else { "Details" },
                Action::Details,
            ),
            ("Refresh ₱", Action::RefreshBalance),
        ],
    );
    if detail_h > 0 {
        if let Some(a) = app.selected_account() {
            let y = controls.y + 1;
            recent_solves(f, a, Rect::new(rect.x, y, rect.width, detail_h), t);
        }
    }
}
fn accounts(f: &mut Frame, app: &mut App, rect: Rect) {
    let t = app.theme;
    let dots = activity_dots(app);
    inline_actions(
        f,
        app,
        Rect::new(rect.x, rect.y, rect.width, 1),
        vec![("+ Add", Action::Add)],
    );
    if app.accounts().is_empty() {
        text_row(
            f,
            Rect::new(rect.x, rect.y + 2, rect.width, 1),
            "Add your Keycash login to begin.",
            t.muted,
        );
        return;
    }
    let body = Rect::new(
        rect.x,
        rect.y + 2,
        rect.width,
        rect.height.saturating_sub(2),
    );
    let height = if body.height >= 5 { 5 } else { 4 };
    let visible = (body.height / height).max(1) as usize;
    let first = app.selected.saturating_sub(visible - 1);
    for i in first..(first + visible).min(app.accounts().len()) {
        let a = app.accounts()[i].clone();
        let y = body.y + (i - first) as u16 * height;
        if y + 3 >= body.bottom() {
            break;
        }
        let r = Rect::new(body.x, y, body.width, height);
        if i == app.selected {
            f.render_widget(
                Block::default().style(Style::default().bg(t.focus_background)),
                r,
            );
        }
        text_row(
            f,
            r,
            format!(
                "{}  {}",
                if i == app.selected { "▸" } else { " " },
                string(&a, "username")
            ),
            t.foreground,
        );
        text_row(
            f,
            Rect::new(r.x, r.y + 1, r.width, 1),
            format!(
                "{} · {}{}",
                task_label(string(&a, "task")),
                daily_limit(&a),
                if string(&a, "proxy_endpoint").is_empty() {
                    String::new()
                } else {
                    format!(" · via {}", string(&a, "proxy_endpoint"))
                }
            ),
            t.muted,
        );
        text_row(
            f,
            Rect::new(r.x, r.y + 2, r.width, 1),
            if !string(&a, "error").is_empty() {
                string(&a, "error").to_owned()
            } else {
                format!(
                    "{} · {}",
                    if truth(&a, "busy") {
                        "Checking login…".to_owned()
                    } else if string(&a, "activity") == "verification" {
                        verification_text(r.width.saturating_sub(10), dots)
                    } else if truth(&a, "active") {
                        "Running".to_owned()
                    } else if truth(&a, "login_saved") {
                        "Login saved".to_owned()
                    } else {
                        "Password needed".to_owned()
                    },
                    if truth(&a, "enabled") {
                        "Enabled"
                    } else {
                        "Excluded"
                    }
                )
            },
            if string(&a, "error").is_empty() {
                if string(&a, "activity") == "verification" {
                    t.accent
                } else {
                    t.muted
                }
            } else {
                t.error
            },
        );
        app.actions
            .push((Rect::new(r.x, r.y, r.width, 3), Action::Account(i)));
        if i == app.selected {
            inline_actions(
                f,
                app,
                Rect::new(r.x, r.y + 3, r.width, 1),
                vec![
                    ("Edit", Action::Edit),
                    (
                        if r.width < 32 { "Login" } else { "Re-login" },
                        Action::Check,
                    ),
                    ("Remove", Action::Remove),
                ],
            );
        }
    }
}
fn settings(f: &mut Frame, app: &mut App, rect: Rect) {
    let t = app.theme;
    let s = &app.snapshot["settings"];
    let proxy = &app.snapshot["proxy"];
    let proxy_enabled = truth(proxy, "enabled");
    let proxy_line = if proxy_enabled {
        format!(
            "Proxy  On · {} ready · {} assigned",
            num(proxy, "ready"),
            num(proxy, "assigned")
        )
    } else if num(proxy, "total") > 0 {
        format!("Proxy  Off · {} saved", num(proxy, "total"))
    } else {
        "Proxy  Off".into()
    };
    let proxy_detail = if !string(proxy, "error").is_empty() {
        format!("       {}", string(proxy, "error"))
    } else if proxy_enabled {
        format!(
            "       {} dead · {} blocked",
            num(proxy, "dead"),
            num(proxy, "blocked")
        )
    } else {
        "       Add proxies in Settings".into()
    };
    let lines = vec![
        Line::raw(format!(
            "Delay  {}–{}s",
            value_text(s, "solve_delay_min", "25"),
            value_text(s, "solve_delay_max", "40")
        )),
        Line::raw(format!(
            "Work   {}–{} min",
            value_text(s, "work_min_minutes", "15"),
            value_text(s, "work_max_minutes", "25")
        )),
        Line::raw(if s["rests_enabled"].as_bool().unwrap_or(true) {
            format!(
                "Rest   {}–{} min",
                value_text(s, "rest_min_minutes", "3"),
                value_text(s, "rest_max_minutes", "7")
            )
        } else {
            "Rest   Disabled".into()
        }),
        Line::raw(format!(
            "Resume {} · Manila",
            value_text(s, "resume_time", "08:00")
        )),
        Line::raw(format!("Theme  {}", app.theme.name)),
        Line::raw(format!(
            "Retry  {} / {}s",
            value_text(s, "network_retries", "3"),
            value_text(s, "retry_delay", "5")
        )),
        Line::raw(format!("Unknown {}", value_text(s, "on_unknown", "stop"))),
        Line::raw(proxy_line),
        Line::styled(proxy_detail, Style::default().fg(t.muted)),
        Line::styled(
            "Pacing applies to all accounts.",
            Style::default().fg(t.muted),
        ),
    ];
    let rows = lines.len().min(rect.height.saturating_sub(2) as usize);
    f.render_widget(
        Paragraph::new(lines.into_iter().take(rows).collect::<Vec<_>>()),
        Rect::new(rect.x, rect.y, rect.width, rows as u16),
    );
    inline_actions(
        f,
        app,
        Rect::new(
            rect.x,
            (rect.y + rows as u16 + 1).min(rect.bottom() - 1),
            rect.width,
            1,
        ),
        vec![
            ("Edit", Action::Settings),
            ("Proxies", Action::Proxy),
            ("Check", Action::ProxyCheck),
        ],
    );
}

fn stats_rate(s: &Value) -> String {
    let n = num(s, "decided");
    if n == 0 {
        "—".into()
    } else {
        format!("{:.1}%", num(s, "correct") as f64 * 100. / n as f64)
    }
}
fn history(f: &mut Frame, app: &mut App, rect: Rect) {
    let t = app.theme;
    let items = list(&app.snapshot, "history").to_vec();
    if items.is_empty() {
        text_row(f, rect, "Account history appears after a run.", t.muted);
        return;
    }
    app.history_selected = app.history_selected.min(items.len() - 1);
    if !app.history_detail {
        text_row(f, rect, "Accounts · all time", t.muted);
        let visible = (rect.height.saturating_sub(2) / 3).max(1) as usize;
        let first = app.history_selected.saturating_sub(visible - 1);
        for (i, h) in items.iter().enumerate().skip(first).take(visible) {
            let y = rect.y + 2 + (i - first) as u16 * 3;
            if y + 1 >= rect.bottom() {
                break;
            }
            let r = Rect::new(rect.x, y, rect.width, 2);
            if i == app.history_selected {
                f.render_widget(
                    Block::default().style(Style::default().bg(t.focus_background)),
                    r,
                );
            }
            let stats = &h["tasks"]["all"]["all"];
            text_row(
                f,
                r,
                format!(
                    "{} {}",
                    if i == app.history_selected {
                        "▸"
                    } else {
                        " "
                    },
                    string(h, "username")
                ),
                t.foreground,
            );
            text_row(
                f,
                Rect::new(r.x, r.y + 1, r.width, 1),
                format!(
                    "{} · {} answers · ₱{}",
                    stats_rate(stats),
                    num(stats, "decided"),
                    value_text(stats, "points", "0")
                ),
                t.success,
            );
            app.actions.push((r, Action::History(i)));
        }
        return;
    }
    let h = items[app.history_selected].clone();
    inline_actions(
        f,
        app,
        Rect::new(rect.x, rect.y, rect.width, 1),
        vec![
            ("Back", Action::HistoryBack),
            ("Clear", Action::HistoryClear),
            (
                if app.history_runs { "Summary" } else { "Runs" },
                Action::HistoryRuns,
            ),
        ],
    );
    text_row(
        f,
        Rect::new(rect.x, rect.y + 1, rect.width, 1),
        string(&h, "username"),
        t.foreground,
    );
    let task = if app.history_task == 0 {
        "all"
    } else {
        TASKS[app.history_task].0
    };
    let label = if app.history_task == 0 {
        "All tasks"
    } else {
        TASKS[app.history_task].1
    };
    inline_actions(
        f,
        app,
        Rect::new(rect.x, rect.y + 2, rect.width, 1),
        vec![(label, Action::HistoryTask)],
    );
    if app.history_runs {
        let runs: Vec<Value> = list(&h, "runs")
            .iter()
            .filter(|r| task == "all" || string(r, "task") == task)
            .cloned()
            .collect();
        if runs.is_empty() {
            text_row(
                f,
                Rect::new(rect.x, rect.y + 4, rect.width, 1),
                "No runs for this task.",
                t.muted,
            );
            return;
        }
        app.run_selected = app.run_selected.min(runs.len() - 1);
        let visible = (rect.height.saturating_sub(5) / 2).max(1) as usize;
        let first = app.run_selected.saturating_sub(visible - 1);
        for (i, r) in runs.iter().enumerate().skip(first).take(visible) {
            let y = rect.y + 3 + (i - first) as u16 * 2;
            if y + 1 >= rect.bottom() - 1 {
                break;
            }
            let area = Rect::new(rect.x, y, rect.width, 2);
            if i == app.run_selected {
                f.render_widget(
                    Block::default().style(Style::default().bg(t.focus_background)),
                    area,
                );
            }
            text_row(
                f,
                area,
                format!(
                    "{} {}",
                    task_label(string(r, "task")),
                    string(r, "started_at").get(..16).unwrap_or("")
                ),
                t.foreground,
            );
            text_row(
                f,
                Rect::new(rect.x, y + 1, rect.width, 1),
                format!(
                    "{} · ₱{} · {} answers",
                    exact_rate(r),
                    value_text(r, "earned_points_total", "0"),
                    num(r, "decided_submissions")
                ),
                t.muted,
            );
            app.actions.push((area, Action::HistoryRun(i)));
        }
        let r = &runs[app.run_selected];
        text_row(
            f,
            Rect::new(rect.x, rect.bottom() - 1, rect.width, 1),
            format!(
                "{} correct / {} wrong / {} unknown",
                num(&r["verdicts"], "correct"),
                num(&r["verdicts"], "incorrect"),
                num(&r["verdicts"], "unknown") + num(&r["verdicts"], "submission_outcome_unknown")
            ),
            t.muted,
        );
    } else {
        let data = &h["tasks"][task];
        let mut y = rect.y + 3;
        for (key, label) in [("today", "Today"), ("week", "7 days"), ("all", "All time")] {
            if y + 1 >= rect.bottom() {
                break;
            }
            let d = &data[key];
            text_row(
                f,
                Rect::new(rect.x, y, rect.width, 1),
                format!(
                    "{} · ₱{} · {}",
                    label,
                    value_text(d, "points", "0"),
                    stats_rate(d)
                ),
                t.success,
            );
            text_row(
                f,
                Rect::new(rect.x, y + 1, rect.width, 1),
                format!(
                    "{} correct / {} wrong / {} unknown",
                    num(d, "correct"),
                    num(d, "incorrect"),
                    num(d, "unknown")
                ),
                t.muted,
            );
            y += 3;
        }
    }
}
fn wrap_proxy_lines(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return vec![];
    }
    let mut lines = Vec::new();
    for line in text.split('\n') {
        if line.is_empty() {
            lines.push(String::new());
            continue;
        }
        let chars: Vec<char> = line.chars().collect();
        for chunk in chars.chunks(width) {
            lines.push(chunk.iter().collect());
        }
    }
    lines
}

fn proxies_dialog(f: &mut Frame, app: &mut App, area: Rect) {
    let t = app.theme;
    let (note, enabled, text, focus, saving) = {
        let d = app.dialog.as_ref().unwrap();
        (
            d.note.clone(),
            d.fields.first().is_some_and(|field| field.value == "true"),
            d.fields
                .get(1)
                .map(|field| field.value.clone())
                .unwrap_or_default(),
            d.focus,
            app.pending_form.is_some(),
        )
    };
    let width = area.width.saturating_sub(2).min(76);
    let height = area.height.saturating_sub(2).min(24);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    f.render_widget(Clear, rect);
    f.render_widget(block("Proxy setup", t), rect);
    let inner = rect.inner(Margin::new(1, 1));
    let parts = Layout::vertical([
        Constraint::Length(2),
        Constraint::Length(1),
        Constraint::Min(4),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .split(inner);

    f.render_widget(
        Paragraph::new(note)
            .wrap(Wrap { trim: true })
            .style(Style::default().fg(t.muted)),
        parts[0],
    );
    let toggle_style = if focus == 0 {
        Style::default().bg(t.focus_background).fg(t.accent)
    } else {
        Style::default().fg(t.foreground)
    };
    f.render_widget(
        Paragraph::new(format!(
            "Use proxies   < {} >",
            if enabled { "Yes" } else { "No" }
        ))
        .style(toggle_style),
        parts[1],
    );
    app.actions.push((parts[1], Action::DialogField(0)));

    let editor_style = if focus == 1 {
        Style::default().bg(t.focus_background)
    } else {
        Style::default()
    };
    f.render_widget(
        block(" Proxies ", t)
            .border_style(Style::default().fg(if focus == 1 { t.accent } else { t.border }))
            .style(editor_style),
        parts[2],
    );
    let editor = parts[2].inner(Margin::new(1, 1));
    let mut lines: Vec<Line> = wrap_proxy_lines(&text, editor.width as usize)
        .into_iter()
        .map(Line::raw)
        .collect();
    if text.trim().is_empty() {
        lines.clear();
        lines.push(Line::styled(
            "Paste one proxy per line",
            Style::default().fg(t.muted),
        ));
    }
    let visible = editor.height.max(1) as usize;
    let start = lines.len().saturating_sub(visible);
    f.render_widget(
        Paragraph::new(lines.into_iter().skip(start).collect::<Vec<_>>()),
        editor,
    );
    app.actions.push((parts[2], Action::DialogField(1)));

    let proxy = &app.snapshot["proxy"];
    let summary = if !string(proxy, "error").is_empty() {
        string(proxy, "error").to_owned()
    } else if parts[3].width < 50 {
        format!(
            "{}R · {}A · {}D · {}B",
            num(proxy, "ready"),
            num(proxy, "assigned"),
            num(proxy, "dead"),
            num(proxy, "blocked")
        )
    } else {
        format!(
            "{} ready · {} assigned · {} dead · {} blocked",
            num(proxy, "ready"),
            num(proxy, "assigned"),
            num(proxy, "dead"),
            num(proxy, "blocked")
        )
    };
    f.render_widget(
        Paragraph::new(summary).style(Style::default().fg(t.muted)),
        parts[3],
    );

    let bw = parts[4].width.min(25);
    let buttons = Layout::horizontal([Constraint::Ratio(1, 2); 2])
        .spacing(1)
        .split(Rect::new(
            parts[4].right() - bw,
            parts[4].y,
            bw,
            parts[4].height,
        ));
    button(
        f,
        app,
        buttons[0],
        if saving { "Saving…" } else { "Save" },
        Action::DialogSave,
    );
    button(f, app, buttons[1], "Cancel", Action::DialogCancel);
}

fn dialog(f: &mut Frame, app: &mut App, area: Rect) {
    if app
        .dialog
        .as_ref()
        .is_some_and(|dialog| dialog.kind == "proxies")
    {
        proxies_dialog(f, app, area);
        return;
    }
    let t = app.theme;
    let d = app.dialog.as_ref().unwrap();
    let confirmation = d.fields.is_empty();
    let width = area
        .width
        .saturating_sub(2)
        .min(if confirmation { 44 } else { 70 });
    let text_width = width.saturating_sub(2).max(1) as usize;
    let mut note_rows = 1u16;
    let mut used = 0usize;
    for word in d.note.split_whitespace() {
        let word_width = unicode_width::UnicodeWidthStr::width(word);
        let gap = usize::from(used > 0);
        if used > 0 && used + gap + word_width > text_width {
            note_rows += 1;
            used = 0;
        }
        used += usize::from(used > 0) + word_width;
    }
    let wanted = if confirmation {
        note_rows + 4
    } else {
        (d.fields.len() * 3 + 5).max(8) as u16
    };
    let height = area.height.saturating_sub(2).min(wanted);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    f.render_widget(Clear, rect);
    let inner = rect.inner(Margin::new(1, 1));
    f.render_widget(block(d.title.clone(), t), rect);
    let parts = Layout::vertical([
        Constraint::Length(if confirmation { note_rows } else { 2 }),
        Constraint::Min(if confirmation { 1 } else { 0 }),
        Constraint::Length(1),
    ])
    .split(inner);
    f.render_widget(
        Paragraph::new(d.note.as_str())
            .wrap(Wrap { trim: true })
            .style(Style::default().fg(t.muted)),
        parts[0],
    );
    let visible = (parts[1].height as usize / 3).max(1);
    let start = d
        .focus
        .min(d.fields.len().saturating_sub(1))
        .saturating_sub(visible - 1);
    let mut hit = vec![];
    for (i, field) in d.fields.iter().enumerate().skip(start).take(visible) {
        let rect = Rect::new(
            parts[1].x,
            parts[1].y + ((i - start) * 3) as u16,
            parts[1].width,
            3,
        );
        let value = if field.password {
            "*".repeat(field.value.chars().count())
        } else if !field.choices.is_empty() {
            format!(
                "< {} >",
                match field.value.as_str() {
                    "true" => "Yes",
                    "false" => "No",
                    "continue" => "Next challenge",
                    "stop" => "Stop",
                    other => task_label(other),
                }
            )
        } else {
            field.value.clone()
        };
        let style = if i == d.focus {
            Style::default().bg(t.focus_background).fg(t.foreground)
        } else {
            Style::default().fg(t.muted)
        };
        f.render_widget(
            Paragraph::new(vec![
                Line::styled(
                    field.label.as_str(),
                    Style::default().fg(if i == d.focus { t.accent } else { t.muted }),
                ),
                Line::raw(if value.is_empty() { "_".into() } else { value }),
            ])
            .style(style),
            rect,
        );
        hit.push((rect, Action::DialogField(i)));
    }
    app.actions.extend(hit);
    let bw = parts[2].width.min(25);
    let button_area = Rect::new(parts[2].right() - bw, parts[2].y, bw, parts[2].height);
    let buttons = Layout::horizontal([Constraint::Ratio(1, 2); 2])
        .spacing(1)
        .split(button_area);
    let saving = app.pending_form.is_some();
    let removing = app
        .dialog
        .as_ref()
        .is_some_and(|d| matches!(d.kind, "remove" | "clear_history"));
    let active = app
        .dialog
        .as_ref()
        .and_then(|d| d.account_id.as_ref())
        .and_then(|uid| app.accounts().iter().find(|a| string(a, "id") == uid))
        .is_some_and(|a| truth(a, "active"));
    button(f, app, buttons[0], "Cancel", Action::DialogCancel);
    button(
        f,
        app,
        buttons[1],
        if saving {
            match app.dialog.as_ref().map(|d| d.kind) {
                Some("clear_history") => "Clearing…",
                Some("remove") => "Removing…",
                _ => "Saving…",
            }
        } else if removing {
            if app
                .dialog
                .as_ref()
                .is_some_and(|d| d.kind == "clear_history")
            {
                "Clear"
            } else {
                "Remove"
            }
        } else if active {
            "Stop & save"
        } else {
            "Save"
        },
        Action::DialogSave,
    );
}
struct TerminalGuard;
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableMouseCapture,
            DisableBracketedPaste,
            LeaveAlternateScreen
        );
    }
}
fn interactive(mut app: App, rx: Option<mpsc::Receiver<String>>) -> Result<()> {
    enable_raw_mode()?;
    let _guard = TerminalGuard;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    terminal.clear()?;
    loop {
        if let Some(rx) = &rx {
            while let Ok(line) = rx.try_recv() {
                app.accept(&line)?;
            }
        }
        if app.closed {
            break;
        }
        terminal.draw(|f| draw(f, &mut app))?;
        if event::poll(Duration::from_millis(80))? {
            match event::read()? {
                Event::Key(key) => app.key(key),
                Event::Paste(text) if app.pending_form.is_none() => {
                    app.paste(&text);
                }
                Event::Mouse(mouse) => {
                    if mouse.kind == MouseEventKind::ScrollDown {
                        app.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
                    } else if mouse.kind == MouseEventKind::ScrollUp {
                        app.key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
                    } else if mouse.kind == MouseEventKind::Down(event::MouseButton::Left) {
                        let action = app
                            .actions
                            .iter()
                            .rev()
                            .find(|(r, _)| r.contains(Position::new(mouse.column, mouse.row)))
                            .map(|(_, a)| a.clone());
                        if let Some(action) = action {
                            app.action(action)
                        }
                    }
                }
                _ => {}
            }
        }
    }
    terminal.show_cursor()?;
    Ok(())
}
pub fn run(
    tx: mpsc::Sender<String>,
    rx: mpsc::Receiver<String>,
    _args: &[String],
    _root: &Path,
) -> Result<()> {
    let mut app = App::new(Some(tx), false);
    let deadline = Instant::now() + Duration::from_secs(30);
    while app.snapshot.get("settings").is_none() {
        let line = rx
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
            .map_err(|_| anyhow::anyhow!("Engine did not load settings; restart the app"))?;
        app.accept(&line)?;
    }
    interactive(app, Some(rx))
}
pub fn demo() -> Result<()> {
    interactive(App::new(None, true), None)
}
fn demo_snapshot() -> Value {
    let accounts:Vec<Value>=[("demo_typing","typing","25835587",59,0,"0.28"),("demo_math","math","green",57,1,"0.43"),("demo_emoji","emoji","🙂 xZNfYN",53,7,"0.84")].into_iter().enumerate().map(|(i,(name,task,prediction,correct,incorrect,points))|json!({"id":i.to_string(),"username":name,"task":task,"enabled":true,"session_saved":true,"daily_target":"1","daily_points":points,"next_resume":"Tomorrow 08:00","login_saved":true,"schedule_mode":"running","cooldown":40,"active":true,"balance":{"amount":"123.88","currency":"PHP","updated_at":"2026-10-18T03:00:00+00:00","checked_local":"11:00","stale":false},"last_result":{"index":5,"verdict":"correct","earned_points":"0.28"},"status":"Waiting","remaining":18,"summary":{"verdicts":{"correct":correct,"incorrect":incorrect},"decided_submissions":correct+incorrect,"earned_points_total":points,"batch_points":19,"batch_size":20},"last":{"answer":prediction,"prediction":prediction,"verdict":"correct","batch_points":19}})).collect();
    let mut accounts = accounts;
    for a in &mut accounts {
        a["batch_points"] = json!(5);
        a["batch_size"] = json!(match string(a, "task") {
            "emoji" => 8,
            _ => 5,
        });
        a["recent_solves"] = json!([
            {"index":20,"task":a["task"],"time_local":"22:31:05","verdict":"correct","earned_points":"0.28","answer":a["last"]["answer"].clone()},
            {"index":19,"task":a["task"],"time_local":"22:30:35","verdict":"correct","earned_points":"0","answer":a["last"]["answer"].clone()},
            {"index":18,"task":a["task"],"time_local":"22:30:05","verdict":"incorrect","answer":"previous-answer"},
            {"index":17,"task":a["task"],"time_local":"22:29:35","verdict":"submission_outcome_unknown"},
            {"index":16,"task":a["task"],"time_local":"22:29:05","verdict":"correct","earned_points":"0","answer":a["last"]["answer"].clone()}
        ]);
    }
    let histories:Vec<Value>=accounts.iter().map(|a|{
        let correct=num(&a["summary"]["verdicts"],"correct");let bad=num(&a["summary"]["verdicts"],"incorrect");
        let stats=json!({"correct":correct,"incorrect":bad,"decided":correct+bad,"unknown":1,"points":a["daily_points"]});
        let days:Vec<Value>=(0..14).map(|i|json!({"date":format!("10/{:02}",i+5),"correct":if i%5==0{0}else{45+i},"incorrect":if i%5==0{0}else{7},"decided":if i%5==0{0}else{52+i},"points":if i%5==0{"0".into()}else{format!("0.{}",30+i)}})).collect();
        let taskdata=json!({"all":stats,"today":stats,"week":stats,"days":days,"recent":[1,1,1,0,1,1,1,1,1,0,1,1,1,1,1,1,1,0,1,1]});
        json!({"id":a["id"],"username":a["username"],"tasks":{"all":taskdata,"typing":taskdata,"math":taskdata,"emoji":taskdata},"runs":[{"username":a["username"],"task":a["task"],"recorded_challenges":correct+bad,"decided_submissions":correct+bad,"verdicts":{"correct":correct,"incorrect":bad},"earned_points_total":a["daily_points"],"started_at":"2026-10-18T03:00:00Z"}]})
    }).collect();
    json!({"settings":{"theme":"keycash","resume_time":"08:00","network_retries":3,"retry_delay":5,"on_unknown":"stop","reduced_motion":false,"proxy_enabled":true},"proxy":{"enabled":true,"total":3,"ready":1,"assigned":1,"dead":1,"blocked":0,"error":"","items":[{"endpoint":"proxy1.example.com:8000","status":"assigned","account_id":"0"},{"endpoint":"proxy2.example.com:8000","status":"ready","account_id":""},{"endpoint":"proxy3.example.com:8000","status":"dead","reason":"Proxy connection failed after 3 attempts","account_id":""}]},"accounts":accounts,"events":["Preview: no live requests"],"history":histories})
}
pub fn render_preview(args: &[String]) -> Result<()> {
    let value = |flag: &str, default: &str| {
        args.iter()
            .position(|x| x == flag)
            .and_then(|i| args.get(i + 1))
            .cloned()
            .unwrap_or(default.into())
    };
    let output = value("--render-preview", "preview.json");
    let width = value("--width", "80").parse::<u16>()?;
    let height = value("--height", "28").parse::<u16>()?;
    let tab = value("--tab", "0").parse::<usize>()?.min(3);
    let mut app = App::new(None, true);
    app.tab = tab;
    if value("--header-state", "") == "recovery" {
        app.snapshot["accounts"][0]["recovering"] = json!(true);
        app.snapshot["accounts"][1]["schedule_mode"] = json!("paused");
    }
    app.selected = if tab == 0 { 2 } else { 0 };
    if value("--state", "") == "empty" {
        app.snapshot["accounts"] = json!([]);
        app.snapshot["history"] = json!([]);
    }
    if value("--state", "") == "resting" {
        app.snapshot["accounts"][app.selected]["wait_kind"] = json!("Resting");
        app.snapshot["accounts"][app.selected]["status"] = json!("Resting");
        app.snapshot["accounts"][app.selected]["remaining"] = json!(272);
    }
    if value("--state", "") == "verifying" {
        let a = &mut app.snapshot["accounts"][app.selected];
        a["activity"] = json!("verification");
        a["verification_steps"] = json!(3);
        a["status"] = json!("Solving human verification");
        a["active"] = json!(true);
        a["scheduled"] = json!(true);
        a["schedule_mode"] = json!("running");
        a["remaining"] = json!(0);
    }
    if value("--state", "") == "waiting" {
        app.snapshot["accounts"][app.selected]["target_reached"] = json!(true);
        app.snapshot["accounts"][app.selected]["daily_points"] = json!("1.0020");
        app.snapshot["accounts"][app.selected]["active"] = json!(false);
        app.snapshot["accounts"][app.selected]["scheduled"] = json!(true);
        app.snapshot["accounts"][app.selected]["schedule_mode"] = json!("waiting");
        app.snapshot["accounts"][app.selected]["status"] = json!("Target reached");
    }
    if value("--state", "") == "ready" {
        let a = &mut app.snapshot["accounts"][app.selected];
        a["active"] = json!(false);
        a["scheduled"] = json!(false);
        a["schedule_mode"] = json!("stopped");
        a["status"] = json!("Ready");
        a["last"] = json!({});
    }
    if ["stopped", "paused"].contains(&value("--state", "").as_str()) {
        let paused = value("--state", "") == "paused";
        app.snapshot["accounts"][app.selected]["active"] = json!(paused);
        app.snapshot["accounts"][app.selected]["scheduled"] = json!(paused);
        app.snapshot["accounts"][app.selected]["schedule_mode"] =
            json!(if paused { "paused" } else { "stopped" });
        app.snapshot["accounts"][app.selected]["status"] =
            json!(if paused { "Paused" } else { "Stopped" });
    }
    if value("--details", "") == "true" {
        app.expanded = true;
    }
    if value("--history-view", "") == "detail" {
        app.history_detail = true;
    }
    if value("--history-view", "") == "runs" {
        app.history_detail = true;
        app.history_runs = true;
    }
    if value("--dialog", "") == "account" {
        app.account_dialog(false);
    }
    if value("--dialog", "") == "remove" {
        app.tab = 1;
        app.confirm("remove");
    }
    if value("--dialog", "") == "clear-history" {
        app.tab = 3;
        app.history_detail = true;
        app.confirm("clear_history");
    }
    if value("--dialog", "") == "settings" {
        app.settings_dialog();
    }
    if value("--dialog", "") == "proxies" {
        app.tab = 2;
        app.proxy_dialog(
            "proxy1.example.com:8000:user1:pass1\nproxy2.example.com:8000:user2:pass2\nproxy3.example.com:8000:user3:pass3\n"
                .into(),
            true,
        );
    }
    if value("--focus", "") == "save" {
        if let Some(d) = &mut app.dialog {
            d.focus = d.fields.len();
        }
    }
    if let Ok(seconds) = value("--remaining", "").parse::<u64>() {
        for a in app.snapshot["accounts"]
            .as_array_mut()
            .into_iter()
            .flatten()
        {
            a["remaining"] = json!(seconds);
        }
    }
    app.theme = theme::find(&value("--theme", "keycash"));
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height))?;
    terminal.draw(|f| draw(f, &mut app))?;
    let buffer = terminal.backend().buffer();
    let cells:Vec<Value>=buffer.content.iter().map(|c|json!({"symbol":c.symbol(),"fg":format!("{:?}",c.fg),"bg":format!("{:?}",c.bg),"bold":c.modifier.contains(Modifier::BOLD)})).collect();
    std::fs::write(
        output,
        serde_json::to_vec(&json!({"width":width,"height":height,"cells":cells}))?,
    )?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_module_renders_on_phone_and_desktop() {
        for (w, h) in [
            (28, 14),
            (30, 16),
            (36, 20),
            (42, 26),
            (80, 24),
            (80, 28),
            (120, 40),
        ] {
            for tab in 0..4 {
                let mut app = App::new(None, true);
                app.tab = tab;
                let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
                terminal.draw(|f| draw(f, &mut app)).unwrap();
                assert!(
                    terminal
                        .backend()
                        .buffer()
                        .content
                        .iter()
                        .any(|c| c.symbol() == "K")
                );
            }
        }
    }
    #[test]
    fn header_counts_are_exclusive_and_recovery_clears_on_mobile() {
        let mut app = App::new(None, true);
        app.snapshot["accounts"][0]["recovering"] = json!(true);
        app.snapshot["accounts"][1]["recovering"] = json!(true);
        app.snapshot["accounts"][1]["schedule_mode"] = json!("paused");
        for width in [10, 24, 62] {
            let rows = header_status(&app, width);
            let text = |line: &Line| {
                line.spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            };
            assert!(rows.iter().all(|line| line.width() <= width as usize));
            assert!(text(&rows[0]).contains('2') && text(&rows[0]).contains('1'));
            assert_eq!(
                text(&rows[1]),
                if width == 10 { "1 retry" } else { "1 retrying" }
            );
        }
        app.snapshot["accounts"][0]["recovering"] = json!(false);
        let rows = header_status(&app, 10);
        assert_eq!(rows[1].spans[0].content, "Ready");
    }

    #[test]
    fn proxy_editor_accepts_large_bulk_paste_with_focus_fallback() {
        let sample = "10.0.0.1:6001:user:pass1\n\
10.0.0.2:6002:user:pass1\n\
10.0.0.3:6003:user:pass1\n\
10.0.0.4:6004:user:pass1\n\
10.0.0.5:6005:user:pass1\n\
10.0.0.6:6006:user:pass1\n\
10.0.0.7:6007:user:pass1\n\
10.0.0.8:6008:user:pass1\n\
10.0.0.9:6009:user:pass1\n\
10.0.0.10:6010:user:pass1";
        let mut app = App::new(None, true);
        app.proxy_dialog(String::new(), true);
        app.action(Action::DialogField(0));
        app.paste(sample);
        let value = &app.dialog.as_ref().unwrap().fields[1].value;
        assert!(value.starts_with("10.0.0.1:6001"));
        assert!(value.ends_with("10.0.0.10:6010:user:pass1"));
        assert_eq!(value.lines().count(), 10);

        let mut crlf = App::new(None, true);
        crlf.proxy_dialog(String::new(), true);
        crlf.paste(&sample.replace('\n', "\r\n"));
        let value = &crlf.dialog.as_ref().unwrap().fields[1].value;
        assert_eq!(value.lines().count(), 10);
        assert!(value.contains("\n10.0.0.2"));
    }
    #[test]
    fn proxy_editor_wraps_long_lines_to_width() {
        assert_eq!(
            wrap_proxy_lines("proxy.example.com:8000:user:pass", 12),
            vec!["proxy.exampl", "e.com:8000:u", "ser:pass"]
        );
        assert_eq!(wrap_proxy_lines("a\n\nb", 8), vec!["a", "", "b"]);
    }
    #[test]
    fn proxy_settings_and_editor_render_on_phone_and_desktop() {
        for (w, h) in [(42, 26), (80, 30)] {
            let mut app = App::new(None, true);
            app.tab = 2;
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Proxy"));
            assert!(text.contains("Proxies"));

            app.proxy_dialog(
                "proxy1.example.com:8000:user1:pass1\nproxy2.example.com:8000:user2:pass2\n".into(),
                true,
            );
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Proxy setup"));
            assert!(text.contains("Use proxies"));
            assert!(text.contains("Proxies"));
            assert!(!text.contains("╭proxy1.example.com"));
            assert!(text.contains("Save"));
            assert!(text.contains("Cancel"));
        }
    }
    #[test]
    fn proxy_editor_accepts_multiline_paste_and_saves_it() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.proxy_dialog(String::new(), true);
        app.action(Action::DialogField(1));
        for c in
            "proxy1.example.com:8000:user1:pass1\nproxy2.example.com:8000:user2:pass2\n".chars()
        {
            app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        let text = &app.dialog.as_ref().unwrap().fields[1].value;
        assert!(text.contains("proxy1.example.com"));
        assert!(text.contains("proxy2.example.com"));
        app.save_dialog();
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(request["command"], "save_proxies");
        assert_eq!(request["proxy_enabled"], true);
        assert!(
            request["proxy_text"]
                .as_str()
                .unwrap()
                .contains("proxy2.example.com")
        );
    }
    #[test]
    fn proxy_dialog_clears_with_ctrl_u() {
        let mut app = App::new(None, true);
        app.proxy_dialog("one\ntwo\n".into(), true);
        app.action(Action::DialogField(1));
        app.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert!(app.dialog.as_ref().unwrap().fields[1].value.is_empty());
    }
    #[test]
    fn proxy_text_event_opens_the_editor() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.request_proxy_dialog();
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(request["command"], "proxy_config");
        app.accept(
            &json!({
                "type":"proxy_text",
                "id":request["id"],
                "text":"proxy.example.com:8000:user:pass\n",
                "enabled":true
            })
            .to_string(),
        )
        .unwrap();
        let dialog = app.dialog.as_ref().unwrap();
        assert_eq!(dialog.kind, "proxies");
        assert!(dialog.fields[1].value.contains("proxy.example.com"));
    }
    #[test]
    fn account_password_is_masked_in_render() {
        let mut app = App::new(None, true);
        app.tab = 1;
        app.account_dialog(false);
        app.dialog.as_mut().unwrap().fields[1].value = "secret-password".into();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(80, 30)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(!text.contains("secret-password"));
        assert!(text.contains("***************"));
    }
    #[test]
    fn modal_typing_does_not_trigger_run_or_quit() {
        let mut app = App::new(None, true);
        app.account_dialog(false);
        app.key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE));
        assert!(!app.closed);
        assert_eq!(app.dialog.as_ref().unwrap().fields[0].value, "q");
    }
    #[test]
    fn modal_captures_clicks_instead_of_starting_a_background_run() {
        let mut app = App::new(None, true);
        app.tab = 0;
        app.account_dialog(false);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(42, 26)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        assert!(!app.actions.is_empty());
        assert!(app.actions.iter().all(|(_, action)| matches!(
            action,
            Action::DialogField(_) | Action::DialogSave | Action::DialogCancel
        )));
    }
    #[test]
    fn save_waits_for_validation_and_preserves_fields_on_error() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.account_dialog(false);
        app.dialog.as_mut().unwrap().fields[0].value = "alice".into();
        app.dialog.as_mut().unwrap().fields[1].value = "private".into();
        app.save_dialog();
        app.save_dialog();
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert!(rx.try_recv().is_err());
        assert!(app.dialog.is_some());
        app.accept(&json!({"type":"result","id":request["id"],"ok":false,"message":"Solves: enter a number"}).to_string()).unwrap();
        let d = app.dialog.as_ref().unwrap();
        assert_eq!(d.fields[0].value, "alice");
        assert!(d.fields[1].value.is_empty());
        assert!(d.note.contains("Solves"));
        assert!(app.pending_form.is_none());
    }
    #[test]
    fn saved_account_is_selected_before_login_finishes() {
        let (tx, _rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.account_dialog(false);
        app.save_dialog();
        app.accept(&json!({"type":"snapshot","settings":{"theme":"keycash"},"accounts":[{"id":"a","username":"alice"},{"id":"b","username":"bob"}]}).to_string()).unwrap();
        app.accept(&json!({"type":"account_saved","id":1,"account_id":"b"}).to_string())
            .unwrap();
        assert_eq!(app.selected, 1);
        assert_eq!(app.tab, 1);
        assert!(app.dialog.is_none());
        app.accept(
            &json!({"type":"result","id":1,"ok":false,"message":"Account saved. Login failed"})
                .to_string(),
        )
        .unwrap();
        assert_eq!(app.accounts().len(), 2);
        assert!(app.error);
    }
    #[test]
    fn enter_on_last_account_field_saves() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.account_dialog(false);
        app.dialog.as_mut().unwrap().focus = 4;
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(request["command"], "save_account");
        assert_eq!(request["enabled"], true);
    }
    #[test]
    fn form_save_and_cancel_are_keyboard_focusable_on_all_sizes() {
        for (w, h) in [(28, 14), (30, 16), (42, 26), (120, 40)] {
            let mut app = App::new(None, true);
            app.account_dialog(false);
            let n = app.dialog.as_ref().unwrap().fields.len();
            for _ in 0..n {
                app.key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
            }
            assert_eq!(app.dialog.as_ref().unwrap().focus, n);
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Save"));
            assert!(text.contains("Cancel"));
            for (a, (rect, _)) in app.actions.iter().enumerate() {
                assert_eq!(rect.intersection(Rect::new(0, 0, w, h)), *rect);
                for (other, _) in app.actions.iter().skip(a + 1) {
                    assert!(rect.intersection(*other).area() == 0);
                }
            }
            app.key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
            app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert!(app.dialog.is_none());
        }
    }
    #[test]
    fn entering_a_new_time_replaces_the_old_value() {
        for (input, expected) in [
            ("0645", "06:45"),
            ("06:45", "06:45"),
            ("0800", "08:00"),
            ("8:00", "08:00"),
        ] {
            let mut app = App::new(None, true);
            app.settings_dialog();
            app.action(Action::DialogField(1));
            for c in input.chars() {
                app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
            }
            assert_eq!(app.dialog.as_ref().unwrap().fields[1].value, expected);
            app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert_eq!(app.dialog.as_ref().unwrap().focus, 2);
        }
    }
    #[test]
    fn live_cards_keep_points_and_answer_below_name_with_one_control_row() {
        for (mode, active, primary, buttons) in [
            ("stopped", false, "start", 1),
            ("running", true, "pause", 2),
            ("paused", true, "resume", 2),
        ] {
            let mut app = App::new(None, true);
            app.selected = 0;
            app.snapshot["accounts"][0]["active"] = json!(active);
            app.snapshot["accounts"][0]["scheduled"] = json!(active);
            app.snapshot["accounts"][0]["schedule_mode"] = json!(mode);
            app.snapshot["accounts"][0]["status"] = json!(mode);
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(36, 20)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let rows: Vec<String> = terminal
                .backend()
                .buffer()
                .content
                .chunks(36)
                .map(|row| row.iter().map(|c| c.symbol()).collect())
                .collect();
            let y = rows.iter().position(|r| r.contains("demo_typing")).unwrap();
            assert!(rows[y + 1].contains("Wallet"));
            assert!(rows[y + 1].contains("₱0.28/1"));
            assert!(rows[y + 2].contains("5/5"));
            assert!(rows[y + 2].contains("25835587"));
            let controls: Vec<_> = app
                .actions
                .iter()
                .filter(|(_, a)| matches!(a, Action::AccountAction(0, _)))
                .collect();
            assert_eq!(controls.len(), buttons);
            assert!(
                controls
                    .iter()
                    .all(|(rect, _)| rect.y == y as u16 && rect.height == 1)
            );
            assert!(controls.iter().any(|(_,a)|matches!(a,Action::AccountAction(_,inner) if match primary{"start"=>matches!(inner.as_ref(),Action::Start(true)),"pause"=>matches!(inner.as_ref(),Action::Pause(false)),_=>matches!(inner.as_ref(),Action::Resume(false))})));
        }
    }
    #[test]
    fn smallest_live_card_keeps_answer_and_countdown_unit() {
        let mut app = App::new(None, true);
        app.selected = 2;
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(28, 14)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let rows: Vec<String> = terminal
            .backend()
            .buffer()
            .content
            .chunks(28)
            .map(|row| row.iter().map(|c| c.symbol()).collect())
            .collect();
        assert!(
            rows.iter()
                .any(|r| r.contains("🙂") && r.contains("xZNfYN") && r.contains("18s"))
        );
    }
    #[test]
    fn verification_activity_replaces_running_and_fits_phone_widths() {
        for width in [28, 42, 80] {
            let mut app = App::new(None, true);
            app.selected = 0;
            let account = &mut app.snapshot["accounts"][0];
            account["activity"] = json!("verification");
            account["verification_steps"] = json!(3);
            account["status"] = json!("Solving human verification");
            account["remaining"] = json!(0);
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, 26)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let rows: Vec<String> = terminal
                .backend()
                .buffer()
                .content
                .chunks(width as usize)
                .map(|row| row.iter().map(|c| c.symbol()).collect())
                .collect();
            let live = rows.iter().find(|r| r.contains("25835587")).unwrap();
            assert!(
                live.contains("erif"),
                "missing status at width {width}: {live}"
            );
            assert!(
                !live.contains("Running"),
                "stale status at width {width}: {live}"
            );
            assert!(
                live.contains("25835587"),
                "missing answer at width {width}: {live}"
            );
        }
    }
    #[test]
    fn card_control_targets_its_own_account() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.snapshot = demo_snapshot();
        for a in app.snapshot["accounts"].as_array_mut().unwrap() {
            a["active"] = json!(false);
            a["scheduled"] = json!(false);
            a["schedule_mode"] = json!("stopped");
            a["status"] = json!("Stopped");
        }
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(42, 26)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let action = app
            .actions
            .iter()
            .find_map(|(_, a)| match a {
                Action::AccountAction(1, inner)
                    if matches!(inner.as_ref(), Action::Start(true)) =>
                {
                    Some(a.clone())
                }
                _ => None,
            })
            .unwrap();
        app.action(action);
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(request["command"], "start");
        assert_eq!(request["account_id"], "1");
    }
    #[test]
    fn bulk_eligibility_uses_every_account_and_keyboard_matches_buttons() {
        let mut app = App::new(None, true);
        app.snapshot["accounts"][0]["active"] = json!(false);
        app.snapshot["accounts"][0]["schedule_mode"] = json!("stopped");
        app.selected = 0;
        assert!(enabled(&app, &Action::Pause(true)));
        assert!(!enabled(&app, &Action::Pause(false)));
        app.tab = 1;
        app.key(KeyEvent::new(KeyCode::Char('e'), KeyModifiers::NONE));
        assert!(app.dialog.is_some());
    }
    #[test]
    fn repeated_start_taps_do_not_create_duplicate_commands() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.snapshot = demo_snapshot();
        app.snapshot["accounts"][0]["active"] = json!(false);
        app.snapshot["accounts"][0]["schedule_mode"] = json!("stopped");
        app.action(Action::Start(true));
        app.action(Action::Start(true));
        assert!(rx.try_recv().is_ok());
        assert!(rx.try_recv().is_err());
        app.accept(&json!({"type":"result","id":1,"ok":true,"message":"Started"}).to_string())
            .unwrap();
        assert!(app.pending_actions.is_empty());
    }
    #[test]
    fn every_history_view_and_live_details_fit_phone_hit_regions() {
        for (w, h) in [(28, 14), (30, 16), (36, 20), (42, 26), (80, 24), (120, 40)] {
            for state in 0..5 {
                let mut app = App::new(None, true);
                app.tab = if state == 0 { 0 } else { 3 };
                app.expanded = true;
                app.history_detail = state > 1;
                app.history_runs = state == 3;
                let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
                terminal.draw(|f| draw(f, &mut app)).unwrap();
                for (i, (rect, _)) in app.actions.iter().enumerate() {
                    assert_eq!(rect.intersection(Rect::new(0, 0, w, h)), *rect);
                    for (other, _) in app.actions.iter().skip(i + 1) {
                        assert_eq!(
                            rect.intersection(*other).area(),
                            0,
                            "overlapping hit regions"
                        );
                    }
                }
                let text: String = terminal
                    .backend()
                    .buffer()
                    .content
                    .iter()
                    .map(|c| c.symbol())
                    .collect();
                assert!(text.contains("Keycash"));
                assert!(text.contains(env!("CARGO_PKG_VERSION")));
                assert!(!text.contains("Asia/Manila"));
            }
        }
    }
    #[test]
    fn smallest_bulk_toolbar_keeps_resume_label_complete() {
        let mut app = App::new(None, true);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(28, 14)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains("Resume"));
    }
    #[test]
    fn progress_uses_account_values_and_never_renders_null() {
        for size in [15, 20, 25, 30] {
            assert_eq!(
                progress_label(&json!({"batch_points":5,"batch_size":size})),
                format!("5/{}", size)
            );
            assert_eq!(
                progress_label(&json!({"batch_points":null,"batch_size":size})),
                format!("—/{}", size)
            );
            assert_eq!(
                progress_label(&json!({"batch_points":0,"batch_size":size})),
                format!("0/{}", size)
            );
        }
    }
    #[test]
    fn pacing_controls_are_global_and_settings_scroll_to_save() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.account_dialog(false);
        assert!(
            !app.dialog
                .as_ref()
                .unwrap()
                .fields
                .iter()
                .any(|f| f.key == "cooldown" || f.key == "rests_enabled")
        );
        app.settings_dialog();
        for key in [
            "solve_delay_min",
            "solve_delay_max",
            "rests_enabled",
            "work_min_minutes",
            "work_max_minutes",
            "rest_min_minutes",
            "rest_max_minutes",
        ] {
            assert!(
                app.dialog
                    .as_ref()
                    .unwrap()
                    .fields
                    .iter()
                    .any(|f| f.key == key)
            );
        }
        app.dialog
            .as_mut()
            .unwrap()
            .fields
            .iter_mut()
            .find(|f| f.key == "rests_enabled")
            .unwrap()
            .value = "false".into();
        let n = app.dialog.as_ref().unwrap().fields.len();
        app.dialog.as_mut().unwrap().focus = n;
        for (w, h) in [(28, 14), (36, 20), (42, 26), (120, 36)] {
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Save") && text.contains("Cancel"));
        }
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let command: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(command["command"], "settings");
        assert_eq!(command["settings"]["rests_enabled"], false);
    }
    #[test]
    fn resting_is_distinct_from_answer_countdown_and_running_count() {
        let mut app = App::new(None, true);
        app.selected = 0;
        app.snapshot["accounts"][0]["wait_kind"] = json!("Resting");
        app.snapshot["accounts"][0]["status"] = json!("Resting");
        app.snapshot["accounts"][0]["remaining"] = json!(272);
        let rows = header_status(&app, 24);
        let text = |line: &Line| {
            line.spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect::<String>()
        };
        assert!(text(&rows[0]).contains("2 running"));
        assert!(text(&rows[2]).contains("1 resting"));
        for (w, h) in [(28, 14), (36, 20), (42, 26)] {
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Resting 04:32"));
            assert!(!text.contains("272s"));
        }
    }
    #[test]
    fn result_feedback_shows_only_positive_payouts() {
        for value in [json!(null), json!("0"), json!("0.00"), json!(0), json!(-1)] {
            assert!(
                !result_message(&json!({"index":1,"verdict":"correct","earned_points":value}))
                    .contains("₱")
            );
        }
        assert!(
            result_message(&json!({"index":1,"verdict":"correct","earned_points":"0.28"}))
                .contains("+₱0.28")
        );
        let feedback =
            result_message(&json!({"index":1,"verdict":"incorrect","answer":"wrong-word"}));
        assert!(feedback.contains("wrong-word"));
    }
    #[test]
    fn both_detail_layouts_show_recent_results_without_retired_fields() {
        for (w, h) in [(28, 14), (36, 20), (42, 26), (80, 28), (120, 36)] {
            let mut app = App::new(None, true);
            app.expanded = true;
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Last 5"));
            assert!(text.contains("5/5"));
            assert!(!text.contains("Batch "));
            assert!(!text.contains("+₱0 "));
            assert!(!text.contains("Run 100"));
            if h >= 20 {
                assert!(text.contains("22:31:05"));
                assert!(text.contains("22:29:05"));
            } else {
                assert!(text.contains("✓") && text.contains("✗") && text.contains("?"));
            }
        }
    }
    #[test]
    fn header_shows_product_version_and_author() {
        let mut app = App::new(None, true);
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(48, 32)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(text.contains("Keycash Pro v1.1.3 by xnote12"));
    }

    #[test]
    fn new_verdict_updates_message_once_and_keeps_robot_static() {
        let mut app = App::new(None, false);
        let mut snap = demo_snapshot();
        for a in snap["accounts"].as_array_mut().unwrap() {
            a["last_result"] = json!({});
        }
        snap["type"] = json!("snapshot");
        app.accept(&snap.to_string()).unwrap();
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(42, 26)).unwrap();
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        let header = terminal.backend().buffer().content[..42 * 4].to_vec();
        snap["accounts"][0]["last_result"] =
            json!({"index":1,"verdict":"incorrect","decided_at":"2026-10-18T03:00:00Z"});
        app.accept(&snap.to_string()).unwrap();
        assert!(app.message.contains("✗"));
        terminal.draw(|f| draw(f, &mut app)).unwrap();
        assert_eq!(header, terminal.backend().buffer().content[..42 * 4]);
        app.notify("Another notification");
        app.accept(&snap.to_string()).unwrap();
        assert_eq!(app.message, "Another notification");
    }

    #[test]
    fn clear_history_confirms_the_history_account_and_not_the_live_selection() {
        let (tx, rx) = mpsc::channel();
        let mut app = App::new(Some(tx), false);
        app.snapshot = demo_snapshot();
        app.selected = 0;
        app.history_selected = 2;
        app.action(Action::HistoryClear);
        for (w, h) in [(28, 14), (42, 26), (80, 24), (120, 40)] {
            let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| draw(f, &mut app)).unwrap();
            assert_eq!(app.actions.len(), 2);
            assert!(
                app.actions
                    .iter()
                    .all(|(r, _)| r.height == 1 && r.width <= 13)
            );
            assert_eq!(app.actions[0].0.intersection(app.actions[1].0).area(), 0);
        }
        assert!(rx.try_recv().is_err());
        assert_eq!(
            app.dialog.as_ref().unwrap().account_id.as_deref(),
            Some("2")
        );
        app.save_dialog();
        let request: Value = serde_json::from_str(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(request["command"], "clear_history");
        assert_eq!(request["account_id"], "2");
        assert_eq!(request["confirmed"], true);
    }
}
