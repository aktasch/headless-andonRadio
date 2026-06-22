package com.andonlabs.radio

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import java.net.URL

data class Station(val name: String, val url: String)

object StationsRepository {
    private const val STATIONS_URL =
        "https://raw.githubusercontent.com/aktasch/headless-andonRadio/main/stations.json"

    suspend fun fetch(): List<Station> = withContext(Dispatchers.IO) {
        val json = URL(STATIONS_URL).readText()
        val arr = JSONArray(json)
        (0 until arr.length()).map { i ->
            val obj = arr.getJSONObject(i)
            Station(obj.getString("name"), obj.getString("url"))
        }
    }
}
