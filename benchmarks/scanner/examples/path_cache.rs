use anyhow::{ensure, Result};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn scan(command: &str, source: &Path) -> Result<BTreeSet<PathBuf>> {
    Ok(
        deps_infer::c_include_parser::retrieve_c_includes(command, vec![source.to_owned()], None)?
            .into_iter()
            .collect(),
    )
}

fn report(
    name: &str,
    before: BTreeSet<PathBuf>,
    after: BTreeSet<PathBuf>,
    source: &Path,
    header: &Path,
    command: &str,
) -> Result<Value> {
    let expected = BTreeSet::from([source.canonicalize()?, header.canonicalize()?]);
    let reference = Command::new(std::env::current_exe()?)
        .args(["--scan-one", command])
        .arg(source)
        .output()?;
    ensure!(
        reference.status.success(),
        "fresh scanner failed: {:?}",
        reference
    );
    let fresh: BTreeSet<PathBuf> = serde_json::from_slice(&reference.stdout)?;
    ensure!(
        fresh == expected,
        "fresh scanner disagrees with the fixture"
    );
    let missing: BTreeSet<_> = expected.difference(&after).collect();
    let extra: BTreeSet<_> = after.difference(&expected).collect();
    Ok(json!({
        "name": name, "before": before, "after": after,
        "command": command, "fresh_after": fresh, "expected_after": expected,
        "missing_after": missing, "extra_after": extra,
    }))
}

fn probe(root: &Path) -> Result<Value> {
    let mut cases = Vec::new();

    let directory = root.join("appearance");
    fs::create_dir(&directory)?;
    let source = directory.join("main.c");
    let header = directory.join("value.h");
    fs::write(
        &source,
        "#if __has_include(\"value.h\")\n#include \"value.h\"\n#endif\n",
    )?;
    let before = scan("cc -c main.c", &source)?;
    fs::write(&header, "#define VALUE 1\n")?;
    cases.push(report(
        "header-appears",
        before,
        scan("cc -c main.c", &source)?,
        &source,
        &header,
        "cc -c main.c",
    )?);

    let directory = root.join("shadowing");
    let early = directory.join("early");
    let late = directory.join("late");
    fs::create_dir_all(&early)?;
    fs::create_dir(&late)?;
    let source = directory.join("main.c");
    fs::write(&source, "#include <value.h>\n")?;
    fs::write(late.join("value.h"), "#define VALUE 1\n")?;
    let command = shell_words::join([
        "cc".to_owned(),
        format!("-I{}", early.display()),
        format!("-I{}", late.display()),
    ]);
    let before = scan(&command, &source)?;
    let header = early.join("value.h");
    fs::write(&header, "#define VALUE 2\n")?;
    cases.push(report(
        "earlier-header-appears",
        before,
        scan(&command, &source)?,
        &source,
        &header,
        &command,
    )?);

    #[cfg(unix)]
    {
        let directory = root.join("retarget");
        fs::create_dir(&directory)?;
        let source = directory.join("main.c");
        let old = directory.join("old.h");
        let new = directory.join("new.h");
        let link = directory.join("value.h");
        fs::write(&source, "#include \"value.h\"\n")?;
        fs::write(&old, "#define VALUE 1\n")?;
        fs::write(&new, "#define VALUE 2\n")?;
        std::os::unix::fs::symlink(&old, &link)?;
        let before = scan("cc -c main.c", &source)?;
        fs::remove_file(&link)?;
        std::os::unix::fs::symlink(&new, &link)?;
        cases.push(report(
            "symlink-retarget",
            before,
            scan("cc -c main.c", &source)?,
            &source,
            &new,
            "cc -c main.c",
        )?);
    }

    Ok(json!({"schema_version": 1, "mode": "upstream-path-observations", "cases": cases}))
}

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() == 3 && args[0] == "--scan-one" {
        println!(
            "{}",
            serde_json::to_string(&scan(&args[1], Path::new(&args[2]))?)?
        );
        return Ok(());
    }
    ensure!(
        args.is_empty(),
        "usage: path_cache [--scan-one COMMAND SOURCE]"
    );
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let root = std::env::temp_dir().join(format!("nni-paths-{}-{nonce}", std::process::id()));
    fs::create_dir(&root)?;
    let root = root.canonicalize()?;
    let result = probe(&root);
    fs::remove_dir_all(&root)?;
    println!("{}", serde_json::to_string(&result?)?);
    Ok(())
}
