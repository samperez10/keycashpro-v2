use std::{
    env,
    fs::{self, File},
    io::{self, Cursor},
    path::{Path, PathBuf},
    process::{Command, ExitCode},
};
use zip::ZipArchive;

const PAYLOAD: &[u8] = include_bytes!(env!("KEYCASH_PORTABLE_PAYLOAD"));
const PAYLOAD_ID: &str = env!("KEYCASH_PAYLOAD_ID");

fn runtime_base() -> PathBuf {
    env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(env::temp_dir)
        .join("KeycashSolver")
        .join("portable-runtime")
}

fn is_ready(folder: &Path) -> bool {
    folder.join("keycash-solver.exe").is_file()
        && fs::read_to_string(folder.join(".payload-id"))
            .is_ok_and(|value| value.trim() == PAYLOAD_ID)
}

fn extract_payload(destination: &Path) -> io::Result<()> {
    if is_ready(destination) {
        return Ok(());
    }
    let parent = destination
        .parent()
        .ok_or_else(|| io::Error::other("invalid cache path"))?;
    fs::create_dir_all(parent)?;
    let stage = parent.join(format!(".stage-{}-{}", PAYLOAD_ID, std::process::id()));
    if stage.exists() {
        fs::remove_dir_all(&stage)?;
    }
    fs::create_dir(&stage)?;

    let result = (|| -> io::Result<()> {
        let mut archive = ZipArchive::new(Cursor::new(PAYLOAD)).map_err(io::Error::other)?;
        for index in 0..archive.len() {
            let mut entry = archive.by_index(index).map_err(io::Error::other)?;
            let relative = entry
                .enclosed_name()
                .ok_or_else(|| io::Error::other("unsafe path in embedded payload"))?
                .to_path_buf();
            let output = stage.join(relative);
            if entry.is_dir() {
                fs::create_dir_all(&output)?;
                continue;
            }
            if let Some(parent) = output.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut file = File::create(output)?;
            io::copy(&mut entry, &mut file)?;
        }
        fs::write(stage.join(".payload-id"), PAYLOAD_ID)?;
        Ok(())
    })();

    if let Err(error) = result {
        let _ = fs::remove_dir_all(&stage);
        return Err(error);
    }
    if destination.exists() {
        if is_ready(destination) {
            fs::remove_dir_all(&stage)?;
            return Ok(());
        }
        fs::remove_dir_all(destination)?;
    }
    fs::rename(stage, destination)
}

fn run() -> Result<i32, String> {
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    let portable_root = executable
        .parent()
        .ok_or("The portable executable has no parent directory.")?;
    let data = portable_root.join("KeycashSolver-data");
    fs::create_dir_all(&data).map_err(|error| {
        format!(
            "Cannot create portable data folder {}: {error}",
            data.display()
        )
    })?;

    let runtime = runtime_base().join(PAYLOAD_ID);
    extract_payload(&runtime)
        .map_err(|error| format!("Cannot prepare the embedded runtime: {error}"))?;
    let status = Command::new(runtime.join("keycash-solver.exe"))
        .arg("--root")
        .arg(&runtime)
        .arg("--data-dir")
        .arg(&data)
        .args(env::args_os().skip(1))
        .current_dir(&runtime)
        .status()
        .map_err(|error| format!("Cannot start Keycash Solver: {error}"))?;
    Ok(status.code().unwrap_or(1))
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(u8::try_from(code).unwrap_or(1)),
        Err(error) => {
            eprintln!("Keycash Solver Portable: {error}");
            ExitCode::FAILURE
        }
    }
}
