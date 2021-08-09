/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package db.exfacades;

import db.facades.AbstractFacade;
import entity.Mp3item;
import java.util.List;
import javax.ejb.Stateless;
import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;
import javax.persistence.Query;

/**
 *
 * @author creativ
 */

@Stateless
public class Mp3itemFacadeExt extends AbstractFacade<Mp3item> implements Mp3itemFacadeLocalExt {

    
    @PersistenceContext(unitName = "RemoteMP3PlayerPU")
    private EntityManager em;

    String queryStr="SELECT i from Mp3item i where i.name like :keyword or i.artist like :keyword";
    
    @Override
    protected EntityManager getEntityManager() {
        return em;
    }

    public Mp3itemFacadeExt() {
        super(Mp3item.class);
    }

    @Override
    public List<Mp3item> search(String keyword) {
        Query query = em.createQuery(queryStr, Mp3item.class);
        query.setParameter("keyword", "%" + keyword + "%");
        return query.getResultList();

    }
}
