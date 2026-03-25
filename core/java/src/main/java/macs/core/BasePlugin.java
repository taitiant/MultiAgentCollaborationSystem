package macs.core;

public interface BasePlugin {
    void onEvent(Event event, SystemState state);
}
