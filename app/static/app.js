function app() {
  const mods = [
    utilsModule(),
    devicesModule(),
    browserModule(),
    playerModule(),
    sourcesModule(),
    selectionModule(),
  ];

  // Use defineProperties to preserve getters across module boundaries
  const state = {};
  for (const mod of mods) {
    Object.defineProperties(state, Object.getOwnPropertyDescriptors(mod));
  }

  state.init = async function () {
    await this.loadDevices();
    this.startSSE();
    await this.loadSources();
    const saved = localStorage.getItem('nasTune_selectedSourceId');
    if (saved) {
      const id = parseInt(saved, 10);
      if (this.sources.find(s => s.id === id)) await this.pickSource(id);
    }
  };

  return state;
}
