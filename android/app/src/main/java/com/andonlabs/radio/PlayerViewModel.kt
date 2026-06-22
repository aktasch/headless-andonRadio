package com.andonlabs.radio

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
import kotlinx.coroutines.launch

class PlayerViewModel(app: Application) : AndroidViewModel(app) {

    val stations = MutableLiveData<List<Station>>(emptyList())
    val currentIndex = MutableLiveData(0)
    val isPlaying = MutableLiveData(false)

    private val player = ExoPlayer.Builder(app).build()

    init {
        viewModelScope.launch {
            stations.postValue(StationsRepository.fetch())
        }
    }

    fun play(index: Int) {
        val list = stations.value ?: return
        if (index !in list.indices) return
        currentIndex.value = index
        player.setMediaItem(MediaItem.fromUri(list[index].url))
        player.prepare()
        player.play()
        isPlaying.value = true
    }

    fun togglePower() {
        if (player.isPlaying) {
            player.pause()
            isPlaying.value = false
        } else {
            player.play()
            isPlaying.value = true
        }
    }

    fun nextStation() {
        val list = stations.value ?: return
        play(((currentIndex.value ?: 0) + 1) % list.size)
    }

    fun prevStation() {
        val list = stations.value ?: return
        play(((currentIndex.value ?: 0) - 1 + list.size) % list.size)
    }

    override fun onCleared() {
        player.release()
        super.onCleared()
    }
}
