/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.facades;

import entity.Playlist;
import javax.ejb.Stateless;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;

/**
 *
 * @author creativ
 */
@Stateless
public class PlaylistFacade extends AbstractFacade<Playlist> implements PlaylistFacadeLocal {
    @PersistenceContext(unitName = "RemoteMP3PlayerPU")
    private EntityManager em;

    @Override
    protected EntityManager getEntityManager() {
        return em;
    }

    public PlaylistFacade() {
        super(Playlist.class);
    }
    
}
