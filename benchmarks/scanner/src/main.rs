use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Deserialize)]
struct CompileCommand {
    directory: PathBuf,
    file: PathBuf,
    command: Option<String>,
    arguments: Option<Vec<String>>,
}

#[derive(Serialize)]
struct ActionResult {
    file: PathBuf,
    command: String,
    elapsed_ns: u128,
    dependencies: BTreeSet<PathBuf>,
    error: Option<String>,
}

#[derive(Serialize)]
struct Report {
    schema_version: u32,
    mode: &'static str,
    directory: PathBuf,
    elapsed_ns: u128,
    actions: Vec<ActionResult>,
}

fn measure(path: &Path) -> Result<Report> {
    let commands: Vec<CompileCommand> = serde_json::from_slice(&std::fs::read(path)?)?;
    let Some(first) = commands.first() else {
        bail!("compilation database is empty");
    };
    let directory = first.directory.canonicalize()?;
    for entry in &commands {
        if entry.directory.canonicalize()? != directory {
            bail!("use one working directory per run; upstream path caching is process-wide");
        }
    }
    std::env::set_current_dir(&directory)?;
    let start = Instant::now();
    let mut actions = Vec::with_capacity(commands.len());
    for entry in commands {
        let command = match (entry.command, entry.arguments) {
            (Some(command), _) => command,
            (None, Some(arguments)) if !arguments.is_empty() => shell_words::join(arguments),
            _ => bail!("missing compiler command for {}", entry.file.display()),
        };
        let start = Instant::now();
        let result = deps_infer::c_include_parser::retrieve_c_includes(
            &command,
            vec![entry.file.clone()],
            None,
        );
        let (dependencies, error) = match result {
            Ok(paths) => {
                let paths = paths
                    .into_iter()
                    .map(|path| {
                        path.canonicalize()
                            .with_context(|| path.display().to_string())
                    })
                    .collect::<Result<BTreeSet<_>>>();
                match paths {
                    Ok(paths) => (paths, None),
                    Err(error) => (BTreeSet::new(), Some(format!("{error:#}"))),
                }
            }
            Err(error) => (BTreeSet::new(), Some(format!("{error:#}"))),
        };
        actions.push(ActionResult {
            file: entry.file,
            command,
            elapsed_ns: start.elapsed().as_nanos(),
            dependencies,
            error,
        });
    }
    Ok(Report {
        schema_version: 1,
        mode: "upstream",
        directory,
        elapsed_ns: start.elapsed().as_nanos(),
        actions,
    })
}

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 1 {
        bail!("usage: nni-scanner-bench COMPILE_COMMANDS.json");
    }
    let report = measure(Path::new(&args[0]))?;
    println!("{}", serde_json::to_string(&report)?);
    if report.actions.iter().any(|action| action.error.is_some()) {
        std::process::exit(1);
    }
    Ok(())
}
