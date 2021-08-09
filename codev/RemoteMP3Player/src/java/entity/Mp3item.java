/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package entity;

import java.io.Serializable;
import java.util.Collection;
import javax.persistence.Basic;
import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.FetchType;
import javax.persistence.GeneratedValue;
import javax.persistence.GenerationType;
import javax.persistence.Id;
import javax.persistence.JoinColumn;
import javax.persistence.JoinTable;
import javax.persistence.ManyToMany;
import javax.persistence.NamedQueries;
import javax.persistence.NamedQuery;
import javax.persistence.Table;
import javax.validation.constraints.Size;
import javax.xml.bind.annotation.XmlRootElement;
import javax.xml.bind.annotation.XmlTransient;

/**
 *
 * @author creativ
 */
@Entity
@Table(name = "mp3item")
@XmlRootElement
@NamedQueries({
    @NamedQuery(name = "Mp3item.findAll", query = "SELECT m FROM Mp3item m"),
    @NamedQuery(name = "Mp3item.findById", query = "SELECT m FROM Mp3item m WHERE m.id = :id"),
    @NamedQuery(name = "Mp3item.findByName", query = "SELECT m FROM Mp3item m WHERE m.name = :name"),
    @NamedQuery(name = "Mp3item.findByArtist", query = "SELECT m FROM Mp3item m WHERE m.artist = :artist"),
    @NamedQuery(name = "Mp3item.findByLyrics", query = "SELECT m FROM Mp3item m WHERE m.lyrics = :lyrics"),
    @NamedQuery(name = "Mp3item.findByPath", query = "SELECT m FROM Mp3item m WHERE m.path = :path")})
public class Mp3item implements Serializable {
    private static final long serialVersionUID = 1L;
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Basic(optional = false)
    @Column(name = "id")
    private Integer id;
    @Size(max = 2147483647)
    @Column(name = "name")
    private String name;
    @Size(max = 2147483647)
    @Column(name = "artist")
    private String artist;
    @Size(max = 2147483647)
    @Column(name = "lyrics")
    private String lyrics;
    @Size(max = 2147483647)
    @Column(name = "path")
    private String path;
    @ManyToMany(mappedBy = "mp3itemCollection", fetch = FetchType.LAZY)
    private Collection<Playlist> playlistCollection;

    public Mp3item() {
    }

    public Mp3item(Integer id) {
        this.id = id;
    }

    public Integer getId() {
        return id;
    }

    public void setId(Integer id) {
        this.id = id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getArtist() {
        return artist;
    }

    public void setArtist(String artist) {
        this.artist = artist;
    }

    public String getLyrics() {
        return lyrics;
    }

    public void setLyrics(String lyrics) {
        this.lyrics = lyrics;
    }

    public String getPath() {
        return path;
    }

    public void setPath(String path) {
        this.path = path;
    }

    @XmlTransient
    public Collection<Playlist> getPlaylistCollection() {
        return playlistCollection;
    }

    public void setPlaylistCollection(Collection<Playlist> playlistCollection) {
        this.playlistCollection = playlistCollection;
    }

    @Override
    public int hashCode() {
        int hash = 0;
        hash += (id != null ? id.hashCode() : 0);
        return hash;
    }

    @Override
    public boolean equals(Object object) {
        // TODO: Warning - this method won't work in the case the id fields are not set
        if (!(object instanceof Mp3item)) {
            return false;
        }
        Mp3item other = (Mp3item) object;
        if ((this.id == null && other.id != null) || (this.id != null && !this.id.equals(other.id))) {
            return false;
        }
        return true;
    }

    @Override
    public String toString() {
        return "entity.Mp3item[ id=" + id + " ]";
    }
    
}
