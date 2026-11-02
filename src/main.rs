use anyhow::{Context, Result};
use pyo3::{prelude::*, types::PyModule};
use std::{
    path::{Path, PathBuf},
    sync::{Mutex, mpsc},
    time::Duration,
};
mod theme;

#[pyclass]
struct Bridge {
    incoming: Mutex<mpsc::Receiver<String>>,
    outgoing: mpsc::Sender<String>,
}
#[pymethods]
impl Bridge {
    fn receive(&self, py: Python<'_>) -> Option<String> {
        py.detach(|| {
            self.incoming
                .lock()
                .ok()?
                .recv_timeout(Duration::from_millis(200))
                .ok()
        })
    }
    fn emit(&self, message: String) {
        let _ = self.outgoing.send(message);
    }
}
fn load_code<'py>(
    py: Python<'py>,
    name: &str,
    source: &str,
    root: &Path,
) -> PyResult<Bound<'py, PyModule>> {
    let module = PyModule::new(py, name)?;
    module.setattr(
        "__file__",
        root.join(format!("{}.py", name.replace('.', "/")))
            .to_string_lossy()
            .as_ref(),
    )?;
    py.import("sys")?
        .getattr("modules")?
        .set_item(name, &module)?;
    py.import("builtins")?
        .getattr("exec")?
        .call1((source, module.dict()))?;
    Ok(module)
}
fn backend<'py>(py: Python<'py>, root: &Path) -> PyResult<Bound<'py, PyModule>> {
    let package = PyModule::new(py, "keycash")?;
    package.setattr("__path__", Vec::<String>::new())?;
    package.setattr("__file__", root.join("keycash/__init__.py"))?;
    py.import("sys")?
        .getattr("modules")?
        .set_item("keycash", &package)?;
    py.import("builtins")?
        .getattr("exec")?
        .call1((include_str!("../keycash/__init__.py"), package.dict()))?;
    for (name, source) in [
        ("keycash.session", include_str!("../keycash/session.py")),
        ("keycash.client", include_str!("../keycash/client.py")),
        (
            "keycash.credentials",
            include_str!("../keycash/credentials.py"),
        ),
        ("keycash.locking", include_str!("../keycash/locking.py")),
        ("keycash.pacing", include_str!("../keycash/pacing.py")),
        ("keycash.games", include_str!("../keycash/games.py")),
        ("keycash.engine", include_str!("../keycash/engine.py")),
    ] {
        load_code(py, name, source, root)?;
    }
    load_code(
        py,
        "keycash.backend",
        include_str!("../keycash/backend.py"),
        root,
    )
}
fn root_path(args: &[String]) -> PathBuf {
    if let Some(i) = args.iter().position(|x| x == "--root") {
        return PathBuf::from(args.get(i + 1).expect("--root requires a path"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(p) = exe.parent() {
            if p.join("build.json").is_file() {
                return p.into();
            }
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}
#[cfg(windows)]
fn configure_portable_python() {
    if let Ok(exe) = std::env::current_exe()
        && let Some(folder) = exe.parent()
    {
        let runtime = folder.join("runtime");
        if runtime.join("python312.zip").is_file() {
            let separator = ";";
            let python_path = format!(
                "{}{}{}{}{}",
                runtime.display(),
                separator,
                runtime.join("python312.zip").display(),
                separator,
                runtime.join("Lib/site-packages").display()
            );
            let path = format!(
                "{}{}{}",
                runtime.display(),
                separator,
                std::env::var_os("PATH")
                    .unwrap_or_default()
                    .to_string_lossy()
            );
            unsafe {
                std::env::set_var("PYTHONHOME", &runtime);
                std::env::set_var("PYTHONPATH", python_path);
                std::env::set_var("PATH", path);
            }
        }
    }
}
#[cfg(not(windows))]
fn configure_portable_python() {}
fn main() -> Result<()> {
    configure_portable_python();
    let args: Vec<String> = std::env::args().collect();
    let root = root_path(&args).canonicalize()?;
    if args.iter().any(|x| x == "--help") {
        println!(
            "Keycash Pro v{} by xnote12\n\nUsage: keycash-solver [--root PATH] [--data-dir PATH]\n       keycash-solver --self-test\n       keycash-solver --render-preview PATH [--width 80] [--height 28]\n\n1-4: pages. q: exit. Start or Resume explicitly after opening the app.",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if args.iter().any(|x| x == "--self-test") {
        Python::attach(|py| -> PyResult<()> {
            let b = backend(py, &root)?;
            let r = b
                .getattr("self_test")?
                .call1((root.to_string_lossy().as_ref(),))?;
            println!("{}", r.extract::<String>()?);
            Ok(())
        })
        .map_err(|e| anyhow::anyhow!("Embedded Python self-test failed: {e}"))?;
        return Ok(());
    }
    if args.iter().any(|x| x == "--render-preview") {
        return ui::render_preview(&args);
    }
    if args.iter().any(|x| x == "--demo") {
        return ui::demo();
    }
    let data = args
        .iter()
        .position(|x| x == "--data-dir")
        .and_then(|i| args.get(i + 1))
        .map(PathBuf::from)
        .unwrap_or(root.join(".state"));
    let (command_tx, command_rx) = mpsc::channel();
    let (event_tx, event_rx) = mpsc::channel();
    let thread_root = root.clone();
    let thread_data = data.clone();
    let error_tx = event_tx.clone();
    let worker = std::thread::spawn(move || {
        let result = Python::attach(|py| -> PyResult<()> {
            let b = backend(py, &thread_root)?;
            let bridge = Py::new(
                py,
                Bridge {
                    incoming: Mutex::new(command_rx),
                    outgoing: event_tx,
                },
            )?;
            b.getattr("serve")?.call1((
                bridge,
                thread_root.to_string_lossy().as_ref(),
                thread_data.to_string_lossy().as_ref(),
            ))?;
            Ok(())
        });
        if let Err(e) = result {
            let _=error_tx.send(serde_json::json!({"type":"fatal","message":format!("Embedded engine failed: {e}")}).to_string());
        }
    });
    let shutdown_tx = command_tx.clone();
    let result = ui::run(command_tx, event_rx, &args, &root);
    let _ = shutdown_tx.send("{\"command\":\"shutdown\"}".into());
    let deadline = std::time::Instant::now() + Duration::from_secs(50);
    while !worker.is_finished() && std::time::Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(100));
    }
    if worker.is_finished() {
        let _ = worker.join();
    }
    result.context("Terminal interface failed")
}
mod ui;
