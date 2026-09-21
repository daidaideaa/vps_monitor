using Workerd = import "/workerd/workerd.capnp";
const config :Workerd.Config = (
  services = [
    (name = "stock-runtime", worker = (
      compatibilityDate = "2026-09-21",
      modules = [
        (name = "test/runtime.js", esModule = embed "runtime.js"),
        (name = "src/index.js", esModule = embed "../src/index.js"),
        (name = "src/parsers.js", esModule = embed "../src/parsers.js")
      ],
      globalOutbound = "merchant"
    )),
    (name = "merchant", worker = (
      compatibilityDate = "2026-09-21",
      modules = [(name = "merchant.js", esModule = embed "runtime-merchant.js")]
    ))
  ]
);
