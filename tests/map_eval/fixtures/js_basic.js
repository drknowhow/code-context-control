const fs = require("fs");
const path = require("path");

const MAX_DEPTH = 8;

function readConfig(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

const joinAll = (...parts) => path.join(...parts);

const walk = async (dir, depth = 0) => {
  if (depth > MAX_DEPTH) return [];
  return fs.readdirSync(dir).map((name) => joinAll(dir, name));
};

class Cache {
  constructor(ttl) {
    this.ttl = ttl;
    this.store = new Map();
  }

  get(key) {
    return this.store.get(key);
  }

  set(key, value) {
    this.store.set(key, value);
    return this;
  }
}

function main() {
  const cache = new Cache(60);
  cache.set("config", readConfig("config.json"));
  return cache;
}

module.exports = { readConfig, walk, Cache, main };
